"""Small in-memory test store."""
REQUEST_COUNT = 0

def get_request_count():
    return REQUEST_COUNT

def increment_count():
    global REQUEST_COUNT
    REQUEST_COUNT += 1
    return REQUEST_COUNT
