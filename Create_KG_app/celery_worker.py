from celery_config import create_app, make_celery
import tasks  # 确保 tasks.py 被导入
# from tasks import save_knowledge_graph_from_csv  # 确保导入了任务

app = create_app()
celery = make_celery(app)

if __name__ == '__main__':
    celery.start()
