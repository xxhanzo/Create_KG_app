from celery import Celery
from flask import Flask
from config import Config
from models import db


def make_celery(app):
    celery = Celery(app.import_name, broker=app.config['CELERY_BROKER_URL'])
    celery.conf.update(app.config)
    celery.conf.update(result_backend=app.config['CELERY_RESULT_BACKEND'])
    celery.conf.task_default_queue = 'default'  # 默认队列
    celery.conf.accept_content = ['json']  # 接受的内容类型
    celery.conf.result_accept_content = ['json']  # 结果的内容类型
    celery.conf.task_serializer = 'json'  # 任务序列化的格式
    celery.conf.result_serializer = 'json'  # 结果序列化的格式
    return celery


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    return app
