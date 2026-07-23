from . import admin, buy, fallback, sell, start

routers = [admin.router, start.router, sell.router, buy.router, fallback.router]
