#!/usr/bin/env python
"""Check a plain-language box-mesh description against GEOS node positions.

Reads reference node coordinates from a baseline output directory (restart HDF5
ReferencePosition datasets, all ranks combined, one mesh body per file group)
or from a VTK .vtu/.vtm tree if no restart is present. Predicts nodes of a
structured box from a description JSON:

  {"bodies": [{"name": "mesh1", "x": [x0, x1], "nx": 11,
               "y": [y0, y1], "ny": 11, "z": [z0, z1], "nz": 1}]}

Nodes of a box are placed at equal spacing, i = 0..nx inclusive.
A body with kind "explicit_nodes" lists every node, or lists an (x, y) table extruded
to each entry of z (split nodes may share a predicted position).
A body with kind "box_plus_polylines" adds uniformly spaced nodes along polylines
(coords and n_elements per segment) to a Cartesian box, for a box reservoir with
line-element wells. A body with kind "piecewise_box" places nodes on a Cartesian grid whose spacing
is uniform inside each block: axes x/y/z give edges and an element count per block.
A body with kind "split_notch_plate" is a Cartesian plate with a linearly closing
mid-height notch (mouth half-opening at x start, closed at notch_x_end).
A body with kind "gll_box" uses the same block edges and element counts, but places
degree-`order` Gauss–Lobatto–Legendre nodes inside each element (shared endpoints
once). The tolerance uses the smallest element edge, not the smallest node gap.
A body with kind "inclined_cartesian_wellbore" lists radius_nodes, theta_deg, z,
map_from_radius, outer_half_width and trajectory (two xyz points). Nodes are
built as a vertical well, blended onto the square, then inclined along the
trajectory. A body with kind "radial_cartesian_blend" instead lists radius_nodes (radii
before mapping), theta_deg, z, map_from_radius and outer_half_width: nodes
inside that radius stay circular, and nodes outside are blended radially onto
the square of the given half-width. Each real node is paired with its nearest
predicted node (several real nodes may share one predicted position). A body
passes when every real node lies within 1% of that body's smallest element
size. Prints a short summary and exits 1 on failure.
"""
import argparse, json, os, sys
import numpy as np


def _restart_positions(output_dir):
    import h5py
    bodies = {}
    # One cycle only: parallel ranks share a directory; later cycles repeat the same reference nodes.
    hdf5s = []
    for root, _, files in os.walk(output_dir):
        for fn in files:
            if fn.endswith('.hdf5') and 'restart' in root:
                hdf5s.append(os.path.join(root, fn))
    if not hdf5s:
        for root, _, files in os.walk(output_dir):
            for fn in files:
                if fn.endswith('.hdf5'):
                    hdf5s.append(os.path.join(root, fn))
    if hdf5s:
        cycle = min(os.path.dirname(p) for p in hdf5s)
        hdf5s = [p for p in hdf5s if os.path.dirname(p) == cycle]
    for path in hdf5s:
            fn = os.path.basename(path)
            try:
                h = h5py.File(path, 'r')
            except OSError:
                continue
            def walk(g, prefix=''):
                for k in g.keys():
                    item = g[k]
                    p = f'{prefix}/{k}' if prefix else k
                    if not isinstance(item, h5py.Group):
                        continue
                    if k == 'ReferencePosition' and '__values__' in item and 'nodeManager' in p and 'embedded' not in p:
                        body = 'mesh'
                        parts = p.split('/')
                        if 'MeshBodies' in parts:
                            body = parts[parts.index('MeshBodies') + 1]
                        a = np.array(item['__values__'][()], dtype=float).reshape(-1, 3)
                        bodies.setdefault(body, []).append(a)
                    else:
                        walk(item, p)
            walk(h)
            h.close()
    return {b: np.vstack(v) for b, v in bodies.items()}


def _hmin_from_points(pts):
    """Smallest distance from a point to a different point. n is tiny for these meshes."""
    hmin = np.inf
    for i in range(len(pts)):
        d = np.linalg.norm(pts - pts[i], axis=1)
        d = d[d > 1e-12]
        if len(d):
            hmin = min(hmin, float(d.min()))
    return float(hmin)


def _cpp_round(x):
    """Round half away from zero, matching the C++ round used to pick a mesh axis."""
    import math
    return int(math.floor(x + 0.5)) if x >= 0 else int(math.ceil(x - 0.5))


def _inclined_cartesian_wellbore(body):
    """Vertical cylindrical nodes, blended onto a square, then inclined along a straight trajectory.

    radius_nodes are the radii before the square blend. theta_deg and z are the
    structured node coordinates of that vertical well. Nodes with radius greater
    than map_from_radius are moved radially so the circle of radius outer_half_width
    becomes the square of that half-width. The centerline then runs from
    trajectory[0] to trajectory[1], with z ends taken from z. The outer square is
    held fixed by the elliptical in-plane correction of an inclined wellbore mesh.
    """
    import math
    radii = [float(r) for r in body['radius_nodes']]
    thetas = [float(t) for t in body['theta_deg']]
    zs = [float(z) for z in body['z']]
    r_map = float(body['map_from_radius'])
    half = float(body['outer_half_width'])
    r_inner = float(body.get('inner_radius', radii[0]))
    x_top, y_top, z_top = [float(v) for v in body['trajectory'][0]]
    x_bot, y_bot, z_bot = [float(v) for v in body['trajectory'][1]]
    # The generator pins the trajectory ends to the mesh z-limits.
    z_top, z_bot = min(zs), max(zs)
    # Re-read the horizontal coordinates; z of the trajectory is replaced above.
    x_top, y_top = float(body['trajectory'][0][0]), float(body['trajectory'][0][1])
    x_bot, y_bot = float(body['trajectory'][1][0]), float(body['trajectory'][1][1])
    pts = []
    for z in zs:
        for thdeg in thetas:
            th = math.radians(thdeg)
            mesh_axis = _cpp_round(th * 2.0 / math.pi)
            phi = abs(th - mesh_axis * math.pi / 2.0)
            for r in radii:
                if r > r_map:
                    rout = half / math.cos(phi)
                    scale = (rout - r_map) / (half - r_map)
                    rr = r_map + scale * (r - r_map)
                else:
                    rr = r
                x = rr * math.cos(th)
                y = rr * math.sin(th)
                r_coord = math.hypot(x, y)
                if r_coord < 1e-12:
                    t_coord = 0.0
                elif y >= 0.0:
                    t_coord = math.acos(max(-1.0, min(1.0, x / r_coord)))
                else:
                    t_coord = 2.0 * math.pi - math.acos(max(-1.0, min(1.0, x / r_coord)))
                mesh_axis = _cpp_round(t_coord * 2.0 / math.pi)
                phi = abs(t_coord - mesh_axis * math.pi / 2.0)
                mesh_rout = half / math.cos(phi)
                dx, dy, dz = x_bot - x_top, y_bot - y_top, z_bot - z_top
                dr = math.hypot(dx, dy)
                dl = math.hypot(dr, dz)
                if dr < 1e-12:
                    theta0 = 0.0
                elif dy >= 0.0:
                    theta0 = math.acos(max(-1.0, min(1.0, dx / dr)))
                else:
                    theta0 = 2.0 * math.pi - math.acos(max(-1.0, min(1.0, dx / dr)))
                d_theta = t_coord - theta0
                tan_d = math.tan(d_theta)
                transform = math.sqrt((1.0 + tan_d * tan_d) / (dz * dz / dl / dl + tan_d * tan_d))
                r_tr = r_coord * ((mesh_rout - r_coord) / (mesh_rout - r_inner) * (transform - 1.0) + 1.0)
                x2 = r_tr * math.cos(t_coord)
                y2 = r_tr * math.sin(t_coord)
                z_ratio = (z - z_top) / (z_bot - z_top)
                x_c = x_top + (x_bot - x_top) * z_ratio
                y_c = y_top + (y_bot - y_top) * z_ratio
                x2 += x_c * (mesh_rout - r_coord) / (mesh_rout - r_inner * transform)
                y2 += y_c * (mesh_rout - r_coord) / (mesh_rout - r_inner * transform)
                pts.append((x2, y2, z))
    pred = np.asarray(pts, dtype=float)
    return pred, _hmin_from_points(pred)


def _gll_reference(order):
    """Degree-n Gauss–Lobatto–Legendre nodes on [-1, 1], endpoints included."""
    from numpy.polynomial.legendre import legder, legroots
    order = int(order)
    c = np.zeros(order + 1)
    c[-1] = 1.0
    interior = np.sort(np.real(legroots(legder(c))))
    return np.concatenate(([-1.0], interior, [1.0]))


def _gll_axis(ax, order):
    edges = [float(v) for v in ax['edges']]
    ns = [int(v) for v in ax['n']]
    if len(ns) != len(edges) - 1:
        raise ValueError('gll_box: n must be one shorter than edges')
    xi = _gll_reference(order)
    coords = []
    for i, n in enumerate(ns):
        h = (edges[i + 1] - edges[i]) / n
        for e in range(n):
            left = edges[i] + e * h
            pts = left + (xi + 1.0) / 2.0 * h
            if coords:
                pts = pts[1:]
            coords.extend(pts.tolist())
    return np.asarray(coords, dtype=float)


def _gll_box(body):
    order = int(body['order'])
    xs, ys, zs = _gll_axis(body['x'], order), _gll_axis(body['y'], order), _gll_axis(body['z'], order)

    def emin(ax):
        edges = [float(v) for v in ax['edges']]
        ns = [int(v) for v in ax['n']]
        return min((edges[i + 1] - edges[i]) / ns[i] for i in range(len(ns)))

    hmin = float(min(emin(body['x']), emin(body['y']), emin(body['z'])))
    xx, yy, zz = np.meshgrid(xs, ys, zs, indexing='ij')
    return np.column_stack([xx.ravel(), yy.ravel(), zz.ravel()]), hmin


def _box_nodes(body):
    def axis(span, n):
        return np.linspace(span[0], span[1], int(n) + 1)
    xs, ys, zs = axis(body['x'], body['nx']), axis(body['y'], body['ny']), axis(body['z'], body['nz'])
    dx = np.diff(xs).min() if len(xs) > 1 else np.inf
    dy = np.diff(ys).min() if len(ys) > 1 else np.inf
    dz = np.diff(zs).min() if len(zs) > 1 else np.inf
    hmin = float(min(dx, dy, dz))
    xx, yy, zz = np.meshgrid(xs, ys, zs, indexing='ij')
    return np.column_stack([xx.ravel(), yy.ravel(), zz.ravel()]), hmin


def _box_plus_polylines(body):
    """Cartesian box nodes plus nodes spaced uniformly along each polyline.

    Each polyline has ``coords`` (vertices) and ``n_elements`` (one integer, or one
    integer per segment). Segment i is divided into n_elements[i] equal elements,
    and the shared endpoints are included once. Used for a box reservoir with
    well trajectories drilled as line elements.
    """
    pts, hmin = _box_nodes(body)
    extras = []
    for pl in body.get('polylines', []):
        coords = np.asarray(pl['coords'], dtype=float)
        n_el = pl['n_elements']
        if isinstance(n_el, (int, float)):
            n_list = [int(n_el)] * (len(coords) - 1)
        else:
            n_list = [int(v) for v in n_el]
        if len(n_list) != len(coords) - 1:
            raise ValueError('box_plus_polylines: n_elements must match the number of segments')
        nodes = [coords[0]]
        for s, n in enumerate(n_list):
            a, b = coords[s], coords[s + 1]
            if n < 1:
                raise ValueError('box_plus_polylines: n_elements must be positive')
            hmin = min(hmin, float(np.linalg.norm(b - a) / n))
            for i in range(1, n + 1):
                nodes.append(a + (b - a) * (i / n))
        extras.append(np.asarray(nodes, dtype=float))
    if extras:
        pts = np.vstack([pts, *extras])
    return pts, hmin


def _interface_graded_box(body):
    """Structured hex nodes graded onto a planar interface y = y_at_x0 + slope*(x-x0).

    x and z are uniform. At each x-node the y-nodes divide [y0, y_interface] into
    n_below equal elements and [y_interface, y1] into n_above equal elements.
    Duplicate nodes that a fracture split places on the interface are not predicted
    twice; the checker allows several real nodes at one predicted position.
    """
    x0, x1 = [float(v) for v in body['x']]
    z0, z1 = [float(v) for v in body['z']]
    y0, y1 = [float(v) for v in body['y']]
    nx, nz = int(body['nx']), int(body['nz'])
    n_below, n_above = int(body['n_below']), int(body['n_above'])
    y_at_x0 = float(body['y_at_x0'])
    slope = float(body['slope'])
    xs = np.linspace(x0, x1, nx + 1)
    zs = np.linspace(z0, z1, nz + 1)
    pts = []
    dy_min = np.inf
    for x in xs:
        y_int = y_at_x0 + slope * (x - x0)
        if not (y0 < y_int < y1):
            raise ValueError('interface_graded_box: interface leaves the y-interval')
        yb = np.linspace(y0, y_int, n_below + 1)
        ya = np.linspace(y_int, y1, n_above + 1)[1:]
        dy_min = min(dy_min, float(np.diff(yb).min()), float((y1 - y_int) / n_above))
        for z in zs:
            for y in np.concatenate([yb, ya]):
                pts.append((float(x), float(y), float(z)))
    hmin = float(min((x1 - x0) / nx, (z1 - z0) / nz, dy_min))
    return np.asarray(pts, dtype=float), hmin


def _split_notch_plate(body):
    """Cartesian plate with a notch that closes linearly along x.

    x, y, z are [start, end] and nx, ny, nz are element counts. Nodes are
    equally spaced in x and z. For x at or beyond notch_x_end, y-nodes are
    equally spaced. For x before the tip, the line y = notch_y is split.
    The half-opening falls linearly from mouth_half_opening at x[0] to zero
    at notch_x_end. n_below equal elements fill the lower side and n_above
    equal elements fill the upper side. Duplicate nodes are not emitted at
    the closed tip.
    """
    x0, x1 = [float(v) for v in body['x']]
    y0, y1 = [float(v) for v in body['y']]
    z0, z1 = [float(v) for v in body['z']]
    nx, ny, nz = int(body['nx']), int(body['ny']), int(body['nz'])
    notch_x_end = float(body['notch_x_end'])
    notch_y = float(body['notch_y'])
    mouth_half = float(body['mouth_half_opening'])
    n_below = int(body.get('n_below', ny // 2))
    n_above = int(body.get('n_above', ny - n_below))
    xs = np.linspace(x0, x1, nx + 1)
    zs = np.linspace(z0, z1, nz + 1)
    pts = []
    dy_min = np.inf
    span = notch_x_end - x0
    for x in xs:
        if x < notch_x_end - 1e-12:
            half = mouth_half * (notch_x_end - float(x)) / span
            y_lo = notch_y - half
            y_hi = notch_y + half
            yb = np.linspace(y0, y_lo, n_below + 1)
            ya = np.linspace(y_hi, y1, n_above + 1)
            ys = np.concatenate([yb, ya])
            dy_min = min(dy_min, float(np.diff(yb).min()), float(np.diff(ya).min()))
        else:
            ys = np.linspace(y0, y1, ny + 1)
            dy_min = min(dy_min, float((y1 - y0) / ny))
        for z in zs:
            for y in ys:
                pts.append((float(x), float(y), float(z)))
    hmin = float(min((x1 - x0) / nx, (z1 - z0) / nz, dy_min))
    return np.asarray(pts, dtype=float), hmin


def _explicit_nodes(body):
    """Node positions given directly, or an (x, y) table extruded to each z.

    Several mesh nodes may share one predicted position (a split interface).
    """
    if 'xy' in body:
        zs = [float(z) for z in body['z']]
        pts = [(float(x), float(y), z) for z in zs for x, y in body['xy']]
    else:
        pts = [tuple(map(float, p)) for p in body['nodes']]
    pred = np.asarray(pts, dtype=float)
    return pred, _hmin_from_points(pred)


def predict(body):
    kind = body.get('kind', 'box')
    if kind == 'explicit_nodes':
        return _explicit_nodes(body)
    if kind == 'split_notch_plate':
        return _split_notch_plate(body)
    if kind == 'interface_graded_box':
        return _interface_graded_box(body)
    if kind == 'box_plus_polylines':
        return _box_plus_polylines(body)
    if kind == 'gll_box':
        return _gll_box(body)
    if kind == 'inclined_cartesian_wellbore':
        return _inclined_cartesian_wellbore(body)
    if kind == 'radial_cartesian_blend':
        # Structured (radius, azimuth, z) nodes, then blended radially from a
        # circle at map_from_radius onto a square outer boundary of half-width
        # outer_half_width. Same placement rule as a cylindrical wellbore mesh
        # whose outer block is Cartesian-aligned. radius_nodes are the radii
        # BEFORE that blend.
        radii = np.asarray(body['radius_nodes'], dtype=float)
        thetas = np.asarray(body['theta_deg'], dtype=float)
        zs = np.asarray(body['z'], dtype=float)
        r_map = float(body['map_from_radius'])
        half = float(body['outer_half_width'])
        pts = []
        for z in zs:
            for thdeg in thetas:
                th = np.deg2rad(thdeg)
                mesh_axis = int(np.round(th * 2.0 / np.pi))
                phi = abs(th - mesh_axis * np.pi / 2.0)
                for r in radii:
                    if r > r_map:
                        rout = half / np.cos(phi)
                        scale = (rout - r_map) / (half - r_map)
                        rr = r_map + scale * (r - r_map)
                    else:
                        rr = r
                    pts.append((rr * np.cos(th), rr * np.sin(th), z))
        pred = np.asarray(pts, dtype=float)
        return pred, _hmin_from_points(pred)
    if kind == 'piecewise_box':
        # Cartesian nodes on a block-wise uniform grid.
        # axes: {"x": {"edges": [x0, x1, ...], "n": [n0, n1, ...]}, "y": ..., "z": ...}
        # Block i runs from edges[i] to edges[i+1] with n[i] equal elements.
        def coords(ax):
            edges = [float(v) for v in ax['edges']]
            ns = [int(v) for v in ax['n']]
            if len(ns) != len(edges) - 1:
                raise ValueError('piecewise_box: n must be one shorter than edges')
            out = [edges[0]]
            for i, n in enumerate(ns):
                out.extend(np.linspace(edges[i], edges[i + 1], n + 1)[1:].tolist())
            return np.asarray(out, dtype=float)
        xs, ys, zs = coords(body['x']), coords(body['y']), coords(body['z'])
        dx = np.diff(xs).min() if len(xs) > 1 else np.inf
        dy = np.diff(ys).min() if len(ys) > 1 else np.inf
        dz = np.diff(zs).min() if len(zs) > 1 else np.inf
        hmin = float(min(dx, dy, dz))
        xx, yy, zz = np.meshgrid(xs, ys, zs, indexing='ij')
        pts = np.column_stack([xx.ravel(), yy.ravel(), zz.ravel()])
        return pts, hmin
    return _box_nodes(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--baseline-output', required=True)
    ap.add_argument('--description', required=True)
    a = ap.parse_args()
    desc = json.load(open(a.description))
    real = _restart_positions(a.baseline_output)
    if not real:
        print('FAIL no reference node positions found')
        sys.exit(1)
    worst = 0.0
    ok = True
    for body in desc['bodies']:
        name = body['name']
        if name not in real:
            print(f'FAIL body {name} not in output; have {list(real)}')
            ok = False
            continue
        pred, hmin = predict(body)
        tol = 0.01 * hmin
        pts = real[name]
        # nearest predicted node for each real node
        # chunk to keep memory small
        dmin = np.empty(len(pts))
        for i in range(0, len(pts), 4096):
            chunk = pts[i:i + 4096]
            d = np.linalg.norm(chunk[:, None, :] - pred[None, :, :], axis=2)
            dmin[i:i + len(chunk)] = d.min(axis=1)
        bad = int(np.sum(dmin > tol))
        worst = max(worst, float(dmin.max()) if len(dmin) else 0.0)
        status = 'PASS' if bad == 0 else 'FAIL'
        if bad:
            ok = False
            order = np.argsort(dmin)[-3:]
            print(status, name, 'nnodes', len(pts), 'npred', len(pred), 'hmin', hmin, 'tol', tol,
                  'max_miss', float(dmin.max()), 'n_bad', bad, 'worst_xyz', pts[order[-1]].tolist())
        else:
            print(status, name, 'nnodes', len(pts), 'npred', len(pred), 'hmin', hmin, 'tol', tol, 'max_miss', float(dmin.max()))
    print('overall', 'PASS' if ok else 'FAIL', 'worst_miss', worst)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
