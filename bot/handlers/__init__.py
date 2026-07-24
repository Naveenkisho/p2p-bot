from . import admin, buy, fallback, guard, sell, start

routers = [admin.router, guard.router, start.router, sell.router, buy.router,
           fallback.router]
