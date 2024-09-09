import os


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'you-will-never-guess'
    SQLALCHEMY_DATABASE_URI = 'mysql+pymysql://root:123456@localhost/kg'  # 根据你的用户名、密码和数据库名进行配置
    # SQLALCHEMY_DATABASE_URI = 'mysql+pymysql://root:jiutuai%40123456@localhost:3306/kg'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    CELERY_BROKER_URL = 'redis://localhost:6379/0'
    CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'

    JWT_SECRET_KEY = 'your_jwt_secret_key'  # 添加JWT密钥配置

