"""本地开发入口：python wsgi.py（容器内用 gunicorn 启动本模块的 app）。"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
