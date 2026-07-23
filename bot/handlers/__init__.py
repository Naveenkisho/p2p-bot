from . import admin, buy, sell, start

routers = [admin.router, start.router, sell.router, buy.router]
