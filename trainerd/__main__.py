import uvicorn

from .server import from_env

uvicorn.run(from_env(), host="10.77.7.70", port=7200, proxy_headers=False, access_log=False)
