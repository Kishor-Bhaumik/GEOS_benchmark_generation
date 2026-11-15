import urllib.error, io
def respond(model, messages, tools, tool_choice=None):
    raise urllib.error.HTTPError('http://x', 429, 'Too Many Requests', {}, io.BytesIO(b'{"error":"rate limit"}'))
