from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class DocxInfo(db.Model):
    __tablename__ = 'docx_info'

    id = db.Column(db.Integer, primary_key=True)
    file_name = db.Column(db.String(255), nullable=False)
    upload_date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(50), nullable=False)
    # graph_id = db.Column(db.Integer, nullable=False)  # 新增字段

    def __init__(self, file_name, status):
        self.file_name = file_name
        self.status = status


# 添加用户模型类
class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __init__(self, username, password_hash):
        self.username = username
        self.password_hash = password_hash

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)



class CsvInfo(db.Model):
    __tablename__ = 'csv_info'

    id = db.Column(db.Integer, primary_key=True)
    file_name = db.Column(db.String(255), nullable=False)
    upload_date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(50), nullable=False)
    describe = db.Column(db.String(255), nullable=True)  # 新增描述字段

    def __init__(self, file_name, status, describe=None):
        self.file_name = file_name
        self.status = status
        self.describe = describe


class EntityClassification(db.Model):
    __tablename__ = 'entity_classifications'

    id = db.Column(db.Integer, primary_key=True)
    entity_types = db.Column(db.String(255), nullable=True)  # 改为entity_types字段
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # 保持created_at字段

    def __init__(self, entity_types=None):
        self.entity_types = entity_types or ""  # 如果为None，则替换为空字符串



class ColorMapping(db.Model):
    __tablename__ = 'type_color_mapping'

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(255), nullable=False, unique=True)
    color_id = db.Column(db.Integer, nullable=False, unique=True)

    def __init__(self, type, color_id):
        self.type = type
        self.color_id = color_id


# 实体管理
class Entity(db.Model):
    __tablename__ = 'entities'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    entity_name = db.Column(db.String(255), nullable=False)
    entity_type = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __init__(self, entity_name, entity_type, created_at=None):
        self.entity_name = entity_name
        self.entity_type = entity_type
        self.created_at = created_at or datetime.utcnow()


class RelationshipModel(db.Model):
    __tablename__ = 'relationship_models'

    id = db.Column(db.Integer, primary_key=True)
    relation_name = db.Column(db.String(255), nullable=False)  # 修改为 relation_name
    start_node_type = db.Column(db.String(255), nullable=False)
    end_node_type = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __init__(self, relation_name, start_node_type, end_node_type, created_at=None):
        self.relation_name = relation_name
        self.start_node_type = start_node_type
        self.end_node_type = end_node_type
        if created_at is None:
            created_at = datetime.utcnow()
        self.created_at = created_at


class Relation(db.Model):
    __tablename__ = 'relations'  # 对应你在 MySQL 中的表名

    id = db.Column(db.Integer, primary_key=True)
    relation = db.Column(db.String(255), nullable=False)  # 关系名称
    relation_type = db.Column(db.String(50), nullable=False)  # 关系类型（entity_to_entity 或 entity_attribute）
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __init__(self, relation, relation_type, created_at=None):
        self.relation = relation
        self.relation_type = relation_type
        if created_at is None:
            created_at = datetime.utcnow()
        self.created_at = created_at


class EntityReview(db.Model):
    __tablename__ = 'entity_reviews'

    id = db.Column(db.Integer, primary_key=True)
    entity_name = db.Column(db.String(255), nullable=False)
    source_file = db.Column(db.String(255), nullable=False)
    source_text = db.Column(db.Text, nullable=False)
    review_status = db.Column(db.String(50), nullable=False, default='待审核')
    review_action = db.Column(db.String(50), nullable=True)
    merge_to_entity = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


    def __init__(self, entity_name, source_file, source_text, review_status='待审核', review_action=None, merge_to_entity=None):
        self.entity_name = entity_name
        self.source_file = source_file
        self.source_text = source_text
        self.review_status = review_status
        self.review_action = review_action
        self.merge_to_entity = merge_to_entity