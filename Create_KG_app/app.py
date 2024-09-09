from flask import Flask, request, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from models import db, DocxInfo, User, CsvInfo, EntityClassification, ColorMapping, RelationshipModel, Entity, EntityReview  # 确保导入了 DocxInfo 类
from tasks import process_file, save_knowledge_graph_from_csv
from celery_config import create_app, make_celery
import os
import time
import json
import pandas as pd
from collections import OrderedDict
from collections import defaultdict
from neo4j_connector import Neo4jConnector
from flask_jwt_extended import JWTManager, create_access_token
from neo4j.graph import Node, Relationship
from neo4j import GraphDatabase



app = create_app()
db.init_app(app)
celery = make_celery(app)
# 初始化 JWTManager
jwt = JWTManager(app)


# 更新ColorMapping表的函数
def update_color_mapping():
    with app.app_context():
        # 初始化Neo4jConnector
        neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")
        node_types = neo4j_connector.get_node_types()
        neo4j_connector.close()

        # 获取数据库中已有的type
        existing_types = {color_mapping.type for color_mapping in ColorMapping.query.all()}

        # 找到新的type
        new_types = node_types - existing_types

        # 获取当前最大color_id
        max_color_id = db.session.query(db.func.max(ColorMapping.color_id)).scalar() or 0

        # 创建新的映射
        new_mappings = []
        for node_type in new_types:
            max_color_id = (max_color_id % 10) + 1  # 保证 color_id 在 1-10 之间循环
            new_mappings.append(ColorMapping(type=node_type, color_id=max_color_id))

        # 如果有新映射则插入到数据库
        if new_mappings:
            db.session.bulk_save_objects(new_mappings)
            db.session.commit()


def reset_color_mapping():
    with app.app_context():
        # 获取所有现有的 ColorMapping 记录，按 id 排序
        mappings = ColorMapping.query.order_by(ColorMapping.id).all()

        # 遍历所有记录并重新分配 color_id 在 1-10 范围内循环
        for i, mapping in enumerate(mappings):
            new_color_id = (i % 10) + 1
            mapping.color_id = new_color_id

        # 提交更改到数据库
        db.session.commit()


# 0. 用户登录接口
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get("username", None)
    password = data.get("password", None)

    if not username or not password:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请输入用户名和密码"
            })
        ])
        return jsonify(response), 400

    user = User.query.filter_by(username=username).first()

    if user and user.check_password(password):
        token = create_access_token(identity=user.id)
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "登录成功",
                "username": username,
                "token": token
            })
        ])
        return jsonify(response), 200

    response = OrderedDict([
        ("code", 0),
        ("errMsg", "success"),
        ("data", {
            "message": "用户名或密码错误"
        })
    ])
    return jsonify(response), 401


# 图谱管理：文件训练(5个）
# 01. 上传docx文件的接口
@app.route('/upload', methods=['POST'])
def upload_file():
    # 检查文件是否存在
    if 'file' not in request.files:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请求中没有文件"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    file = request.files['file']

    # 检查文件是否选择
    if file.filename == '':
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "未选择上传文件"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    # 检查文件后缀名是否为docx
    if not file.filename.lower().endswith('.docx'):
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "目前只支持.docx文档的图谱生成"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    try:
        file_path = os.path.abspath(os.path.join('generate_data', file.filename))
        file.save(file_path)

        # 在数据库中保存文件记录
        new_file = DocxInfo(file_name=file.filename, status='Uploaded')
        db.session.add(new_file)
        db.session.commit()

        # 返回上传成功的信息
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "id": new_file.id,
                "file_name": new_file.file_name,
                "upload_date": new_file.upload_date.strftime('%Y-%m-%d %H:%M:%S'),
                "status": new_file.status
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    except Exception as e:
        # 处理文件保存或数据库保存中的错误
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"上传文件时发生错误: {str(e)}"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')


# 图谱管理：文件训练：
# 02. 生成知识图谱接口
@app.route('/generate_graph', methods=['POST'])
def generate_graph():
    data = request.json
    graph_id = data.get("id")

    if not graph_id:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请输入需要生成的文件id"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    try:
        # 查找文件记录
        file_record = DocxInfo.query.get(graph_id)
        if not file_record:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": f"没有ID为: {graph_id} 的文件"
                })
            ])
            return Response(json.dumps(response), mimetype='application/json')

        # 检查图谱是否已经生成
        if file_record.status == 'Completed':
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": f"ID为 {graph_id} 已经生成"
                })
            ])
            return Response(json.dumps(response), mimetype='application/json')

        # 更新状态为 'Generating Graph'
        file_record.status = 'Generating Graph'
        db.session.commit()

        # 启动后台任务生成图谱
        file_path = os.path.abspath(os.path.join('generate_data', file_record.file_name))
        process_file.delay(graph_id, file_path)

        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "开始创建知识图谱"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    except Exception as e:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"An error occurred: {str(e)}"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')


# 图谱管理：文件训练：
# 03.查看docx文件的状态
@app.route('/status', methods=['GET'])
def get_status():
    # 获取分页参数
    page_num = int(request.args.get('page_num', 1))
    page_size = int(request.args.get('page_size', 10))

    # 获取所有文件记录，按上传时间降序排列
    query = DocxInfo.query.order_by(DocxInfo.upload_date.desc())

    # 计算总页数
    total = query.count()

    # 获取当前页的记录
    file_records = query.paginate(page=page_num, per_page=page_size, error_out=False).items

    # 构建文件状态列表
    file_status = [
        {
            "file_id": file.id,
            "file_name": file.file_name,
            "status": file.status,
            "upload_date": file.upload_date.strftime('%Y-%m-%d %H:%M:%S')
        }
        for file in file_records
    ]

    # 构建有序的返回数据结构
    response = OrderedDict([
        ("code", 0),
        ("errMsg", "success"),
        ("data", {
            "file_status": file_status,
            "page_num": page_num,
            "page_size": page_size,
            "total": total
        })
    ])

    return Response(json.dumps(response), mimetype='application/json')


# 图谱管理：文件训练：
# 04. 删除docx文件
@app.route('/delete', methods=['GET'])
def delete_file():
    file_id = request.args.get('id')

    if not file_id:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请输入需要删除的文件ID"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json'), 400

    try:
        # 查找要删除的文件记录
        file_record = DocxInfo.query.get(file_id)
        if not file_record:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": "未找到该文件"
                })
            ])
            return Response(json.dumps(response), mimetype='application/json'), 404

        # 删除与文件记录相关的文件（如生成的数据）
        file_path = os.path.join('generate_data', file_record.file_name)
        if os.path.exists(file_path):
            os.remove(file_path)

        # 删除与该文件ID相关的所有生成文件
        base_filename = os.path.splitext(file_record.file_name)[0]
        glm_responses_file = os.path.join('generate_data', f'{base_filename}_responses.json')
        triples_csv_file = os.path.join('generate_data', f'{base_filename}_triples.csv')
        filtered_prompts_file = os.path.join('generate_data', 'filtered_prompts.json')

        if os.path.exists(glm_responses_file):
            os.remove(glm_responses_file)

        if os.path.exists(triples_csv_file):
            os.remove(triples_csv_file)

        if os.path.exists(filtered_prompts_file):
            os.remove(filtered_prompts_file)

        # 删除数据库中的记录
        db.session.delete(file_record)
        db.session.commit()

        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "文件删除成功！"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    except Exception as e:
        db.session.rollback()
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": str(e)
            })
        ])
        return Response(json.dumps(response), mimetype='application/json'), 500


# 图谱管理：文件训练：
# 05. 修改docx文档name接口
@app.route('/modify_docx_name', methods=['POST'])
def modify_docx_name():
    data = request.json
    docx_id = data.get("id")
    new_name = data.get("new_name")

    # 检查是否缺少参数
    if not docx_id or not new_name:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "缺少必要的参数"
            })
        ])
        return jsonify(response), 400

    # 检查新名称是否以 ".docx" 结尾
    if not new_name.lower().endswith('.docx'):
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "文件名必须以 '.docx' 结尾"
            })
        ])
        return jsonify(response), 400

    # 查找对应的 DOCX 记录
    docx_record = DocxInfo.query.get(docx_id)
    if not docx_record:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"未找到ID为 {docx_id} 的DOCX文件记录"
            })
        ])
        return jsonify(response), 404

    # 构建原始文件路径
    old_file_path = os.path.join('generate_data', docx_record.file_name)

    # 构建新的文件路径
    new_file_path = os.path.join('generate_data', new_name)

    try:
        # 修改文件名称
        if os.path.exists(old_file_path):
            os.rename(old_file_path, new_file_path)

        # 更新数据库记录中的文件名称
        docx_record.file_name = new_name
        db.session.commit()

        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "DOCX文件名称修改成功",
                "updated_item": {
                    "id": docx_id,
                    "new_name": new_name
                }
            })
        ])
        return jsonify(response), 200

    except Exception as e:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"修改DOCX文件名称时发生错误: {str(e)}"
            })
        ])
        return jsonify(response), 500


# 图谱管理：文件导入（5个）
# 01. 上传csv文件接口
@app.route('/upload_csv', methods=['POST'])
def upload_csv():
    if 'file' not in request.files:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请求中没有文件"
            })
        ])
        return jsonify(response), 400

    file = request.files['file']

    if file.filename == '':
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "未选择上传文件"
            })
        ])
        return jsonify(response), 400

    if not file.filename.lower().endswith('.csv'):
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "目前只支持.csv文件的图谱生成"
            })
        ])
        return jsonify(response), 400

    describe = request.form.get('description', '').strip()

    try:
        # 检查并创建文件夹
        upload_folder = os.path.abspath(os.path.join(os.getcwd(), 'generate_data'))
        if not os.path.exists(upload_folder):
            os.makedirs(upload_folder)

        # 读取 CSV 文件为 DataFrame
        csv_df = pd.read_csv(file)

        # 期望的列名
        expected_columns = [
            "Head", "Relation", "Tail",
            "Head Type", "Tail Type",
            "Head Major Classification", "Head Minor Classification",
            "Tail Major Classification", "Tail Minor Classification",
            "Relation Type"
        ]

        # 验证列名是否一致
        if list(csv_df.columns) != expected_columns:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": "CSV文件格式不正确，请确保列名和顺序为：'Head', 'Relation', 'Tail', 'Head Type', 'Tail Type', 'Head Major Classification', 'Head Minor Classification', 'Tail Major Classification', 'Tail Minor Classification', 'Relation Type'"
                })
            ])
            return jsonify(response), 400

        # 生成唯一的文件名
        base_name, ext = os.path.splitext(file.filename)
        unique_file_name = f"{base_name}_{int(time.time())}{ext}"
        file_path = os.path.join(upload_folder, unique_file_name)

        # 将内容保存到文件
        file.seek(0)  # 确保从文件开头开始读取
        file_content = file.read()
        with open(file_path, 'wb') as f:
            f.write(file_content)

        # 将CSV文件记录保存到数据库
        new_csv = CsvInfo(file_name=unique_file_name, describe=describe, status='Uploaded')
        db.session.add(new_csv)
        db.session.commit()

        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "id": new_csv.id,
                "file_name": new_csv.file_name,
                "describe": new_csv.describe,
                "upload_date": new_csv.upload_date.strftime('%Y-%m-%d %H:%M:%S'),
                "status": new_csv.status
            })
        ])
        return jsonify(response), 200

    except Exception as e:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"上传文件时发生错误: {str(e)}"
            })
        ])
        return jsonify(response), 500


# 图谱管理：文件导入
# 02. 根据csv文件来生成知识图谱
@app.route('/generate_graph_fromcsv', methods=['POST'])
def generate_knowledge_graph():
    data = request.json
    csv_id = data.get("id")

    if not csv_id:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请输入CSV文件的ID"
            })
        ])
        return jsonify(response), 400

    # 从数据库中查找CSV文件记录
    csv_info = CsvInfo.query.get(csv_id)
    if not csv_info:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"未找到ID为 {csv_id} 的CSV文件记录"
            })
        ])
        return jsonify(response), 404

    # 检查CSV文件是否已经生成过知识图谱
    if csv_info.status == 'Completed':
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"CSV文件ID {csv_id} 已经生成过知识图谱，无需再次生成",
                "id": csv_id,
                "status": csv_info.status
            })
        ])
        return jsonify(response), 200

    # 构建文件路径
    file_path = os.path.abspath(os.path.join('generate_data', csv_info.file_name))
    if not os.path.exists(file_path):
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"CSV文件 '{csv_info.file_name}' 不存在"
            })
        ])
        return jsonify(response), 404

    # 更新状态为 'Generating Graph'
    csv_info.status = 'Generating Graph'
    db.session.commit()

    # 启动异步任务生成知识图谱
    save_knowledge_graph_from_csv.delay(csv_id, file_path)

    response = OrderedDict([
        ("code", 0),
        ("errMsg", "success"),
        ("data", {
            "message": f"知识图谱生成任务已启动, CSV文件ID: {csv_id}",
            "id": csv_id,
            "status": csv_info.status
        })
    ])
    return jsonify(response), 200


# 图谱管理：文件导入
# 03. 删除csv文件接口
@app.route('/delete_csv', methods=['GET'])
def delete_csv_file():
    csv_id = request.args.get('id')

    if not csv_id:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请输入需要删除的CSV文件ID"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json'), 400

    try:
        # 查找要删除的CSV文件记录
        csv_record = CsvInfo.query.get(csv_id)
        if not csv_record:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": "未找到该CSV文件"
                })
            ])
            return Response(json.dumps(response), mimetype='application/json'), 404

        # 构建文件路径
        file_path = os.path.join('generate_data', csv_record.file_name)
        if os.path.exists(file_path):
            os.remove(file_path)

        # 删除数据库中的记录
        db.session.delete(csv_record)
        db.session.commit()

        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "CSV文件删除成功！"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    except Exception as e:
        db.session.rollback()
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": str(e)
            })
        ])
        return Response(json.dumps(response), mimetype='application/json'), 500


# 图谱管理：文件导入
# 04. 修改csv文件接口
@app.route('/modify_csv_name', methods=['POST'])
def modify_csv_name():
    data = request.json
    csv_id = data.get("id")
    new_name = data.get("new_name")
    new_description = data.get("describe")  # 获取新的描述

    # 检查是否缺少参数
    if not csv_id or not new_name:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "缺少必要的参数"
            })
        ])
        return jsonify(response), 400

    # 如果新名称不以 ".csv" 结尾，则自动添加 ".csv"
    if not new_name.lower().endswith('.csv'):
        new_name += '.csv'

    # 查找对应的CSV记录
    csv_record = CsvInfo.query.get(csv_id)
    if not csv_record:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"未找到ID为 {csv_id} 的CSV文件记录"
            })
        ])
        return jsonify(response), 404

    # 构建原始文件路径
    old_file_path = os.path.join('generate_data', csv_record.file_name)

    # 构建新的文件路径
    new_file_path = os.path.join('generate_data', new_name)

    try:
        # 修改文件名称
        if os.path.exists(old_file_path):
            os.rename(old_file_path, new_file_path)

        # 更新数据库记录中的文件名称和描述
        csv_record.file_name = new_name
        if new_description is not None:  # 如果提供了描述，则更新描述
            csv_record.describe = new_description
        db.session.commit()

        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "CSV文件名称和描述修改成功",
                "updated_item": {
                    "id": csv_id,
                    "new_name": new_name,
                    "describe": new_description
                }
            })
        ])
        return jsonify(response), 200

    except Exception as e:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"修改CSV文件名称和描述时发生错误: {str(e)}"
            })
        ])
        return jsonify(response), 500


# 图谱管理：文件导入
# 05. 查看csv文件状态接口
@app.route('/csv_status', methods=['GET'])
def get_csv_status():
    # 获取分页参数
    page_num = int(request.args.get('page_num', 1))
    page_size = int(request.args.get('page_size', 10))

    # 获取所有 CSV 文件记录，按上传时间降序排列
    query = CsvInfo.query.order_by(CsvInfo.upload_date.desc())

    # 计算总记录数
    total = query.count()

    # 获取当前页的记录
    file_records = query.paginate(page=page_num, per_page=page_size, error_out=False).items

    # 构建文件状态列表
    file_status = [
        {
            "file_id": file.id,
            "file_name": file.file_name.rsplit('.', 1)[0],  # 去除 .csv 后缀
            "describe": file.describe,  # 包含描述字段
            "status": file.status,
            "upload_date": file.upload_date.strftime('%Y-%m-%d %H:%M:%S')
        }
        for file in file_records
    ]

    # 构建有序的返回数据结构
    response = OrderedDict([
        ("code", 0),
        ("errMsg", "success"),
        ("data", {
            "file_status": file_status,
            "page_num": page_num,
            "page_size": page_size,
            "total": total
        })
    ])

    return Response(json.dumps(response), mimetype='application/json')


# 图谱管理：实体管理
# 01. 查询所有节点 (实体名称+实体类型)
@app.route('/get_all_entities', methods=['GET'])
def get_all_entities():
    try:
        # 获取分页参数
        page_num = int(request.args.get('page_num', 1))
        page_size = int(request.args.get('page_size', 10))

        # 查询 MySQL 数据库中的所有实体
        query = Entity.query

        # 获取总记录数
        total = query.count()

        # 获取当前页的记录
        entity_records = query.paginate(page=page_num, per_page=page_size, error_out=False).items

        # 构建实体列表
        entities = []
        for entity in entity_records:
            entities.append({
                "id": entity.id,
                "name": entity.entity_name,
                "classification": entity.entity_type
            })

        # 返回查询结果
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "success",
                "entities": entities,
                "page_num": page_num,
                "page_size": page_size,
                "total": total
            })
        ])
        return jsonify(response), 200

    except Exception as e:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"查询实体时发生错误: {str(e)}"
            })
        ])
        return jsonify(response), 500


# 图谱管理：实体管理
# 02. 新增实体
@app.route('/add_entities', methods=['POST'])
def add_entity():
    # 获取请求数据
    data = request.json
    entity_name = data.get("entity_name", "").strip()
    entity_type_id = data.get("entity_type")

    # 检查输入的有效性
    if not entity_name or entity_type_id is None:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "缺少必要的字段"
            }
        }
        return jsonify(response), 400

    try:
        # 查询 entity_classifications 表中对应的 entity_type
        classification = EntityClassification.query.get(entity_type_id)
        if not classification:
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": f"未找到 ID 为 {entity_type_id} 的分类"
                }
            }
            return jsonify(response), 404

        # 新建 Entity 实体
        new_entity = Entity(
            entity_name=entity_name,
            entity_type=classification.entity_types,  # 使用查询到的 entity_types
            created_at=datetime.utcnow()
        )

        # 添加到数据库会话并提交
        db.session.add(new_entity)
        db.session.commit()

        # 返回成功响应
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "id": new_entity.id,
                "entity_name": new_entity.entity_name,
                "entity_type": new_entity.entity_type,
                "created_at": new_entity.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "message": "实体已成功添加"
            }
        }
        return jsonify(response), 200

    except Exception as e:
        # 捕获异常并返回错误响应
        db.session.rollback()
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"添加实体时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500


# 图谱管理：实体管理
# 03. 删除mysql中的实体
@app.route('/delete_entity_mysql', methods=['POST'])
def delete_entity_mysql():
    # 获取请求数据
    data = request.json
    entity_id = data.get("id")

    # 检查是否提供了实体ID
    if not entity_id:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "缺少实体ID"
            }
        }
        return jsonify(response), 400

    try:
        # 查找要删除的实体记录
        entity = Entity.query.get(entity_id)
        if not entity:
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": f"未找到ID为 {entity_id} 的实体记录"
                }
            }
            return jsonify(response), 404

        # 删除实体记录
        db.session.delete(entity)
        db.session.commit()

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"ID为 {entity_id} 的实体已成功删除"
            }
        }
        return jsonify(response), 200

    except Exception as e:
        db.session.rollback()
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"删除实体时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500


# 图谱管理：实体管理
# 04. 编辑mysql中的实体
@app.route('/edit_entity_mysql', methods=['POST'])
def edit_entity_mysql():
    # 获取请求数据
    data = request.json
    entity_id = data.get("id")
    new_entity_name = data.get("entity_name", "").strip()
    new_entity_type_id = data.get("entity_type")

    # 检查请求数据的有效性
    if not entity_id or not new_entity_name or not new_entity_type_id:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "缺少必要的字段"
            }
        }
        return jsonify(response), 400

    try:
        # 查找要修改的实体记录
        entity_record = Entity.query.get(entity_id)
        if not entity_record:
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": f"未找到ID为 {entity_id} 的实体记录"
                }
            }
            return jsonify(response), 404

        # 获取新的实体类型
        new_entity_type_record = EntityClassification.query.get(new_entity_type_id)
        if not new_entity_type_record:
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": f"未找到ID为 {new_entity_type_id} 的实体类型"
                }
            }
            return jsonify(response), 404

        # 更新实体信息
        entity_record.entity_name = new_entity_name
        entity_record.entity_type = new_entity_type_record.entity_types

        # 提交更改
        db.session.commit()

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "id": entity_record.id,
                "entity_name": entity_record.entity_name,
                "entity_type": entity_record.entity_type,
                "created_at": entity_record.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "message": "实体信息已成功更新"
            }
        }
        return jsonify(response), 200

    except Exception as e:
        db.session.rollback()
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"更新实体信息时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500


# 03. 删除实体
@app.route('/delete_entity', methods=['POST'])
def delete_entity():
    data = request.json
    node_id = data.get("id")

    if not node_id:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请提供需要删除的节点ID"
            })
        ])
        return jsonify(response), 400

    neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

    try:
        # 删除节点及其关联的所有关系
        success = neo4j_connector.delete_node_and_relationships(node_id)

        if success:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": f"节点及其关联关系已成功删除，节点ID: {node_id}"
                })
            ])
            return jsonify(response), 200
        else:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": f"未找到ID为 {node_id} 的节点"
                })
            ])
            return jsonify(response), 404

    except Exception as e:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"删除节点时发生错误: {str(e)}"
            })
        ])
        return jsonify(response), 500

    finally:
        neo4j_connector.close()


# 图谱管理：实体管理
# 02. 修改实体和关系名称接口
@app.route('/modify_element', methods=['POST'])
def modify_element():
    data = request.json
    entity_id = data.get("id")
    new_name = data.get("new_name")
    entity_type = data.get("type")  # "node" or "relationship"

    # 检查是否缺少必要参数
    if not entity_id or not new_name or not entity_type:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "缺少必要的参数: 'id', 'new_name', 或 'type'"
            })
        ])
        return jsonify(response), 400

    # 检查 entity_type 是否有效
    if entity_type not in ["node", "relationship"]:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "无效的 'type' 参数，必须是 'node' 或 'relationship'"
            })
        ])
        return jsonify(response), 400

    try:
        neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")
        success = False

        # 根据类型更新名称
        if entity_type == "node":
            success = neo4j_connector.update_node_name(entity_id, new_name)
        elif entity_type == "relationship":
            success = neo4j_connector.update_relationship_name(entity_id, new_name)

        neo4j_connector.close()

        # 检查更新是否成功
        if success:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": "success",
                    "updated_item": {
                        "type": entity_type,
                        "id": entity_id,
                        "new_name": new_name
                    }
                })
            ])
            return jsonify(response), 200
        else:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": f"{entity_type.capitalize()} 未找到，ID: {entity_id}"
                })
            ])
            return jsonify(response), 404

    except ConnectionError:
        # 处理与 Neo4j 的连接错误
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "无法连接到 Neo4j 数据库，请检查连接配置。"
            })
        ])
        return jsonify(response), 500

    except ValueError as ve:
        # 处理值错误，例如无效的 ID 或名称格式
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"无效的输入值: {str(ve)}"
            })
        ])
        return jsonify(response), 400

    except Exception as e:
        # 捕获所有其他异常
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"发生未知错误: {str(e)}"
            })
        ])
        return jsonify(response), 500


# 图谱管理：图谱数据
# 01. 获取素有分类（下拉列表）
@app.route('/get_classifications', methods=['GET'])
def get_classifications():
    try:
        # 查询所有的 entity_types 数据
        entity_types_data = EntityClassification.query.with_entities(EntityClassification.entity_types).all()

        # 处理数据为需要的分类结构
        classifications = defaultdict(lambda: {"label": "", "value": "", "children": []})

        for (entity_type,) in entity_types_data:
            if '-' in entity_type:
                parent, child = entity_type.split('-', 1)
                # 确保主分类存在
                if not classifications[parent]["label"]:
                    classifications[parent]["label"] = parent
                    classifications[parent]["value"] = parent
                # 添加子分类
                classifications[parent]["children"].append({"label": child, "value": child})
            else:
                # 没有 '-' 的情况，作为单独的分类
                if not classifications[entity_type]["label"]:
                    classifications[entity_type]["label"] = entity_type
                    classifications[entity_type]["value"] = entity_type

        # 转换为列表
        classifications_list = list(classifications.values())

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "success",
                "classifications": classifications_list
            }
        }
        return jsonify(response), 200

    except Exception as e:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"获取分类数据时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500

# 图谱管理：图谱数据
# 02. 得到所有关系的下拉列表
@app.route('/get_all_relationship_types', methods=['GET'])
def get_all_relationship_types():
    neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

    try:
        # 从 Neo4j 中查询所有关系的名称
        query = """
        MATCH ()-[r]->() 
        RETURN DISTINCT r.name AS relationship_name
        """
        with neo4j_connector.driver.session() as session:
            result = session.run(query)
            relationships = []
            for record in result:
                relationship_name = record["relationship_name"]
                if relationship_name:  # 过滤掉没有 name 属性的关系
                    relationships.append({
                        "label": relationship_name,
                        "value": relationship_name
                    })

        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "relationships": relationships
            })
        ])
        return jsonify(response), 200

    except Exception as e:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"查询关系类型时发生错误: {str(e)}"
            })
        ])
        return jsonify(response), 500

    finally:
        neo4j_connector.close()


# 图谱管理：图谱数据
# 03. 根据分类查询节点及关系
@app.route('/filter_graph', methods=['POST'])
def filter_graph():
    data = request.json

    # 从 URL 查询参数中获取分页参数
    page_num = int(request.args.get('page_num', 1))
    page_size = int(request.args.get('page_size', 10))

    # 支持按 id 查询
    relation_id = data.get("id")

    start_node_type = data.get("start_node_type", {})
    end_node_type = data.get("end_node_type", {})
    start_node_name = data.get("start_node_name", "")
    end_node_name = data.get("end_node_name", "")
    relationship = data.get("relationship", "")

    neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

    try:
        if relation_id:
            # 如果传入了id，则根据id直接在返回结果中查找
            all_results = neo4j_connector.filter_graph_by_criteria(
                start_node_type, end_node_type, start_node_name, end_node_name, relationship
            )

            # 根据传入的id找到具体的关系
            results = [result for result in all_results if result['id'] == relation_id]

            # 如果没有找到，返回提示信息
            if not results:
                response = {
                    "code": 0,
                    "errMsg": "success",
                    "data": {
                        "message": f"未找到ID为 {relation_id} 的关系记录"
                    }
                }
                return jsonify(response), 404

        else:
            # 否则，使用其他条件进行查询
            results = neo4j_connector.filter_graph_by_criteria(
                start_node_type, end_node_type, start_node_name, end_node_name, relationship
            )

        # 计算总记录数和总页数
        total_records = len(results)

        # 对结果进行分页
        start_index = (page_num - 1) * page_size
        end_index = start_index + page_size
        paginated_results = results[start_index:end_index]

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "success",
                "results": paginated_results,
                "page_num": page_num,
                "page_size": page_size,
                "total": total_records
            }
        }
        return jsonify(response), 200
    except Exception as e:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"筛选图谱时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500
    finally:
        neo4j_connector.close()


@app.route('/edit_graph', methods=['POST'])
def edit_graph():
    data = request.json

    # 必填参数: 关系 ID
    relation_id = data.get("id")

    # 可选参数
    start_node_id = data.get("start_node_name")
    relationship_name = data.get("relationship")  # 现在直接输入关系名称，而不是ID
    end_node_id = data.get("end_node_name")

    if not relation_id:
        return jsonify({
            "code": 0,
            "errMsg": "error",
            "data": {
                "message": "必须提供关系的 ID"
            }
        }), 400

    neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

    try:
        # 获取新节点名称
        start_node_name = None
        end_node_name = None

        if start_node_id:
            start_node = Entity.query.filter_by(id=start_node_id).first()
            if start_node:
                start_node_name = start_node.entity_name

        if end_node_id:
            end_node = Entity.query.filter_by(id=end_node_id).first()
            if end_node:
                end_node_name = end_node.entity_name

        # 更新 Neo4j 中的关系
        neo4j_connector.update_relationship(
            relation_id, start_node_name, relationship_name, end_node_name
        )

        return jsonify({
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "关系更新成功"
            }
        }), 200

    except Exception as e:
        return jsonify({
            "code": 0,
            "errMsg": "error",
            "data": {
                "message": f"更新关系时发生错误: {str(e)}"
            }
        }), 500

    finally:
        neo4j_connector.close()


@app.route('/add_node_relationship', methods=['POST'])
def add_node_relationship():
    data = request.json

    # 获取用户输入的头节点名称、关系名称和尾节点名称
    start_node_id = data.get("start_node_name")
    relationship_name = data.get("relationship")
    end_node_id = data.get("end_node_name")

    # 确保头节点和尾节点的 ID 都存在
    if not start_node_id or not end_node_id or not relationship_name:
        return jsonify({
            "code": 1,
            "errMsg": "输入参数不完整",
            "data": {}
        }), 400

    # 创建 Neo4j 连接
    neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

    try:
        # 查询 MySQL 数据库，获取头节点和尾节点的名称
        start_node = Entity.query.get(start_node_id)
        end_node = Entity.query.get(end_node_id)

        if not start_node or not end_node:
            return jsonify({
                "code": 1,
                "errMsg": "无法找到指定的节点",
                "data": {}
            }), 404

        start_node_name = start_node.entity_name
        end_node_name = end_node.entity_name

        # 在 Neo4j 中创建实体和关系
        neo4j_connector.create_srelationship(
            start_node_name,
            relationship_name,
            end_node_name
        )

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "关系已成功创建",
                "start_node_name": start_node_name,
                "relationship": relationship_name,
                "end_node_name": end_node_name
            }
        }
        return jsonify(response), 201

    except Exception as e:
        response = {
            "code": 1,
            "errMsg": "error",
            "data": {
                "message": f"创建关系时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500

    finally:
        neo4j_connector.close()


# 图谱管理：图谱数据
# 04.根据分类来查询所有节点（新增按钮出的下拉框）
@app.route('/get_classifications_with_entities', methods=['GET'])
def get_classifications_with_entities():
    try:
        # 获取所有实体类型的数据
        entities = Entity.query.with_entities(Entity.entity_name, Entity.entity_type, Entity.id).all()

        # 使用 defaultdict 构建分类层次结构
        classifications = defaultdict(lambda: {"label": "", "value": "", "children": []})

        for entity_name, entity_type, entity_id in entities:
            if '-' in entity_type:
                parent, child = entity_type.split('-', 1)
                # 确保主分类存在
                if not classifications[parent]["label"]:
                    classifications[parent]["label"] = parent
                    classifications[parent]["value"] = parent

                # 检查子分类是否存在于主分类下
                child_node = next((c for c in classifications[parent]["children"] if c["label"] == child), None)
                if not child_node:
                    child_node = {"label": child, "value": child, "children": []}
                    classifications[parent]["children"].append(child_node)

                # 添加具体的实体节点
                child_node["children"].append({"label": entity_name, "value": entity_id})
            else:
                # 没有 '-' 的情况，作为顶层分类
                if not classifications[entity_type]["label"]:
                    classifications[entity_type]["label"] = entity_type
                    classifications[entity_type]["value"] = entity_type

                # 将 entity_name 放在该分类节点下
                classifications[entity_type]["children"].append({"label": entity_name, "value": entity_id})

        # 转换为列表
        classifications_list = list(classifications.values())

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "success",
                "classifications": classifications_list
            }
        }
        return jsonify(response), 200

    except Exception as e:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"获取分类数据时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500


# 知识建模：实体类型管理（4个）
# 01. 新增类型接口
@app.route('/add_classification', methods=['POST'])
def add_classification():
    data = request.json
    classification = data.get("classification", "")

    if not classification:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "缺少分类信息"
            }
        }
        return jsonify(response), 400

    try:
        # 初始化 entity_types
        entity_types = classification.strip()

        # 检查是否已存在相同的分类信息
        existing_classification = EntityClassification.query.filter_by(entity_types=entity_types).first()

        if existing_classification:
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": "该分类信息已存在"
                }
            }
            return jsonify(response), 400

        # 创建新的分类记录
        new_classification = EntityClassification(entity_types=entity_types)

        db.session.add(new_classification)
        db.session.commit()

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "id": new_classification.id,
                "entity_types": new_classification.entity_types,
                "created_at": new_classification.created_at.strftime('%Y-%m-%d %H:%M:%S'),  # 返回创建时间
                "message": "分类信息已添加成功"
            }
        }
        return jsonify(response), 200

    except Exception as e:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"添加分类信息时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500


# 知识建模：实体类型管理
# 02 查看所有类型接口
@app.route('/classification_status', methods=['GET'])
def get_classification_status():
    # 获取分页参数
    page_num = int(request.args.get('page_num', 1))
    page_size = int(request.args.get('page_size', 10))

    # 获取所有分类记录，按id降序排列
    query = EntityClassification.query.order_by(EntityClassification.id.desc())

    # 计算总记录数
    total = query.count()

    # 获取当前页的记录
    classification_records = query.paginate(page=page_num, per_page=page_size, error_out=False).items

    # 构建分类状态列表
    classification_status = []
    for classification in classification_records:
        classification_data = {
            "id": classification.id,
            "classifications": classification.entity_types,
            "created_at": classification.created_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(classification.created_at, datetime) else classification.created_at
        }
        classification_status.append(classification_data)

    # 构建有序的返回数据结构
    response = OrderedDict([
        ("code", 0),
        ("errMsg", "success"),
        ("data", {
            "message": "success",
            "classification_status": classification_status,
            "page_num": page_num,
            "page_size": page_size,
            "total": total
        })
    ])

    return Response(json.dumps(response), mimetype='application/json')


# 知识建模：实体类型管理
# 03 删除类型接口
@app.route('/delete_classification', methods=['GET'])
def delete_classification():
    classification_id = request.args.get('id')

    if not classification_id:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请输入需要删除的类型ID"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json'), 400

    try:
        # 查找要删除的分类记录
        classification_record = EntityClassification.query.get(classification_id)
        if not classification_record:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": f"未找到ID为 {classification_id} 的类型记录"
                })
            ])
            return Response(json.dumps(response), mimetype='application/json'), 404

        # 删除数据库中的记录
        db.session.delete(classification_record)
        db.session.commit()

        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"类型删除成功！ID: {classification_id}"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    except Exception as e:
        db.session.rollback()
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"删除类型记录时发生错误: {str(e)}"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json'), 500


# 知识建模：实体类型管理
# 04 编辑实体类型
@app.route('/edit_classification', methods=['POST'])
def edit_classification():
    data = request.json
    classification_id = data.get("id")
    new_entity_types = data.get("classifications")  # 使用新的字段名 entity_types

    # 检查是否缺少必要参数
    if not classification_id:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "缺少分类ID"
            }
        }
        return jsonify(response), 400

    # 查找要修改的分类记录
    classification_record = EntityClassification.query.get(classification_id)
    if not classification_record:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"未找到ID为 {classification_id} 的分类记录"
            }
        }
        return jsonify(response), 404

    try:
        # 更新分类信息
        if new_entity_types:
            classification_record.entity_types = new_entity_types

        # 提交更改
        db.session.commit()

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "id": classification_record.id,
                "entity_types": classification_record.entity_types,
                "created_at": classification_record.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "message": "分类信息已成功更新"
            }
        }
        return jsonify(response), 200

    except Exception as e:
        db.session.rollback()
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"更新分类信息时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500


# 知识建模：关系管理
# 01. 查询所有关系
@app.route('/get_all_relationships', methods=['GET'])
def get_all_relationships():
    # 获取分页参数
    page_num = int(request.args.get('page_num', 1))
    page_size = int(request.args.get('page_size', 10))

    try:
        # 查询所有关系记录
        query = RelationshipModel.query.order_by(RelationshipModel.id.asc())
        total = query.count()

        # 获取当前页的记录
        relationship_records = query.paginate(page=page_num, per_page=page_size, error_out=False).items

        # 构建关系数据列表
        relationships = []
        for record in relationship_records:
            relationship_data = {
                "relationship_id": record.id,
                "relationship_name": record.relation_name,
                "from": {
                    "classification": record.start_node_type  # 使用start_node_type作为分类
                },
                "to": {
                    "classification": record.end_node_type  # 使用end_node_type作为分类
                }
            }
            relationships.append(relationship_data)

        # 构建有序的返回数据结构
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "success",
                "relationships": relationships,
                "page_num": page_num,
                "page_size": page_size,
                "total": total
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    except Exception as e:
        # 处理异常情况
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"查询关系时发生错误: {str(e)}"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')


# 02. 新增关系及开始、结束节点类型
@app.route('/add_relationship_models', methods=['POST'])
def add_relationship():
    # 获取请求数据
    data = request.json
    relation_name = data.get("relation_name", "").strip()
    start_node_id = str(data.get("start_node_type", "")).strip()  # 将ID转换为字符串后再去除空格
    end_node_id = str(data.get("end_node_type", "")).strip()  # 将ID转换为字符串后再去除空格

    # 检查请求数据的有效性
    if not relation_name or not start_node_id or not end_node_id:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "缺少必要的字段"
            }
        }
        return jsonify(response), 400

    try:
        # 根据传入的ID查找entity_classification表中的entity_types
        start_classification = EntityClassification.query.get(start_node_id)
        end_classification = EntityClassification.query.get(end_node_id)

        # 检查是否找到了对应的分类
        if not start_classification or not end_classification:
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": "无效的节点类型ID"
                }
            }
            return jsonify(response), 400

        # 获取entity_types
        start_node_type = start_classification.entity_types
        end_node_type = end_classification.entity_types

        # 创建新的关系记录
        new_relationship = RelationshipModel(
            relation_name=relation_name,
            start_node_type=start_node_type,
            end_node_type=end_node_type,
            created_at=datetime.utcnow()  # 设置创建时间为当前时间
        )

        # 将新记录添加到数据库会话并提交
        db.session.add(new_relationship)
        db.session.commit()

        # 返回成功响应
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "id": new_relationship.id,
                "relation_name": new_relationship.relation_name,
                "start_node_type": new_relationship.start_node_type,
                "end_node_type": new_relationship.end_node_type,
                "created_at": new_relationship.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "message": "关系已成功添加"
            }
        }
        return jsonify(response), 200

    except Exception as e:
        # 捕获异常并返回错误响应
        db.session.rollback()
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"添加关系时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500


# 知识建模：关系管理
# 03. 关系管理-开始节点与结束节点的下拉框
@app.route('/get_classification_options', methods=['GET'])
def get_classification_options():
    try:
        # 从数据库中查询所有分类的entity_types和id
        classifications = EntityClassification.query.with_entities(EntityClassification.id,
                                                                   EntityClassification.entity_types).distinct().all()

        # 构建分类选项列表
        classification_list = [{"label": classification.entity_types, "value": classification.id} for classification in
                               classifications]

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "classifications": classification_list
            }
        }
        return jsonify(response), 200

    except Exception as e:
        response = {
            "code": 1,
            "errMsg": str(e),
            "data": {
                "message": "获取分类选项时发生错误"
            }
        }
        return jsonify(response), 500


# 知识建模：关系管理
# 04. 编辑关系+开始节点，结束节点的类型
@app.route('/edit_relationship', methods=['POST'])
def edit_relationship():
    # 获取请求数据
    data = request.json
    relationship_id = data.get("relationship_id")
    new_relationship_name = data.get("new_relationship_name", "").strip()
    new_start_node_id = data.get("start_node_classification")
    new_end_node_id = data.get("end_node_classification")

    # 检查请求数据的有效性
    if not relationship_id:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "缺少 relationship_id"
            }
        }
        return jsonify(response), 400

    try:
        # 查找要修改的关系记录
        relationship_record = RelationshipModel.query.get(relationship_id)
        if not relationship_record:
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": f"未找到ID为 {relationship_id} 的关系记录"
                }
            }
            return jsonify(response), 404

        # 根据传入的ID查找 entity_classification 表中的 entity_types
        if new_start_node_id:
            new_start_classification = EntityClassification.query.get(new_start_node_id)
            if not new_start_classification:
                response = {
                    "code": 0,
                    "errMsg": "success",
                    "data": {
                        "message": "无效的开始节点类型ID"
                    }
                }
                return jsonify(response), 400
            relationship_record.start_node_type = new_start_classification.entity_types

        if new_end_node_id:
            new_end_classification = EntityClassification.query.get(new_end_node_id)
            if not new_end_classification:
                response = {
                    "code": 0,
                    "errMsg": "success",
                    "data": {
                        "message": "无效的结束节点类型ID"
                    }
                }
                return jsonify(response), 400
            relationship_record.end_node_type = new_end_classification.entity_types

        # 更新关系信息
        if new_relationship_name:
            relationship_record.relation_name = new_relationship_name

        # 提交更改
        db.session.commit()

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "id": relationship_record.id,
                "relation_name": relationship_record.relation_name,
                "start_node_type": relationship_record.start_node_type,
                "end_node_type": relationship_record.end_node_type,
                "created_at": relationship_record.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "message": "关系信息已成功更新"
            }
        }
        return jsonify(response), 200

    except Exception as e:
        db.session.rollback()
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"更新关系信息时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500


# 知识建模：关系管理
# 05. 删除关系+开始节点，结束节点的类型
@app.route('/delete_relationship_models', methods=['POST'])
def delete_relationship_models():
    data = request.json
    relationship_id = data.get("relationship_id")

    # 检查是否提供了 relationship_id
    if not relationship_id:
        response = {
            "code": 1,
            "errMsg": "缺少relationship_id",
            "data": {
                "message": "relationship_id是必需的"
            }
        }
        return jsonify(response), 400

    try:
        # 查找要删除的关系记录
        relationship = RelationshipModel.query.get(relationship_id)

        # 检查记录是否存在
        if not relationship:
            response = {
                "code": 1,
                "errMsg": "关系不存在",
                "data": {
                    "message": f"未找到ID为 {relationship_id} 的关系记录"
                }
            }
            return jsonify(response), 404

        # 删除记录
        db.session.delete(relationship)
        db.session.commit()

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"ID为 {relationship_id} 的关系记录已成功删除"
            }
        }
        return jsonify(response), 200

    except Exception as e:
        db.session.rollback()  # 回滚事务
        response = {
            "code": 1,
            "errMsg": str(e),
            "data": {
                "message": "删除关系记录时发生错误"
            }
        }
        return jsonify(response), 500


# 首页
# 01. 查询节点
@app.route('/query', methods=['POST'])
def query_node():
    reset_color_mapping()
    update_color_mapping()
    data = request.json
    all_nodes = data.get("all_nodes", False)
    node_name = data.get("name", None)
    docx_id = data.get("docx_id", None)
    csv_id = data.get("csv_id", None)
    neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

    try:
        if all_nodes:
            nodes, relationships = neo4j_connector.find_all_nodes_and_relationships()
            neo4j_connector.close()

            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", OrderedDict([
                    ("message", "success"),
                    ("nodes", nodes),
                    ("relationships", relationships)
                ]))
            ])
            return Response(json.dumps(response), mimetype='application/json')

        if node_name:
            node_data, related_nodes, relationships = neo4j_connector.find_node_and_relationships(node_name, docx_id, csv_id)
            neo4j_connector.close()

            if not node_data:
                response = OrderedDict([
                    ("code", 0),
                    ("errMsg", "success"),
                    ("data", OrderedDict([
                        ("message", "在图谱中未找到该节点")
                    ]))
                ])
                return Response(json.dumps(response), mimetype='application/json')

            # 去重处理
            unique_nodes = {node["id"]: node for node in [node_data] + related_nodes}.values()

            unique_relationships = set()
            filtered_relationships = []
            for relationship in relationships:
                rel_tuple = (relationship["from"], relationship["to"], relationship["text"])
                if rel_tuple not in unique_relationships:
                    unique_relationships.add(rel_tuple)
                    filtered_relationships.append(relationship)

            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", OrderedDict([
                    ("message", "success"),
                    ("nodes", list(unique_nodes)),
                    ("relationships", filtered_relationships)
                ]))
            ])
            return Response(json.dumps(response), mimetype='application/json')

        # 根据docx_id或csv_id查询子图谱
        if docx_id or csv_id:
            nodes, relationships = neo4j_connector.find_nodes_and_relationships_by_docx_or_csv(docx_id, csv_id)
            neo4j_connector.close()

            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", OrderedDict([
                    ("message", "success"),
                    ("nodes", nodes),
                    ("relationships", relationships)
                ]))
            ])
            return Response(json.dumps(response), mimetype='application/json')

        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", OrderedDict([
                ("message", "未输入节点")
            ]))
        ])
        return Response(json.dumps(response), mimetype='application/json')

    except Exception as e:
        neo4j_connector.close()
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"发生错误: {str(e)}"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')


# 首页
# 02. 选择子图谱列表
@app.route('/get_file_list', methods=['GET'])
def get_file_list():
    try:
        # 连接Neo4j，查询所有的docx_id和csv_id
        neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")
        docx_ids = neo4j_connector.get_all_docx_ids()
        csv_ids = neo4j_connector.get_all_csv_ids()
        neo4j_connector.close()

        # 使用docx_ids在MySQL中查询对应的docx文件名
        docx_list = []
        for docx_id in docx_ids:
            docx_info = DocxInfo.query.get(docx_id)
            if docx_info:
                docx_list.append({
                    "label": docx_info.file_name,
                    "value": docx_id
                })

        # 使用csv_ids在MySQL中查询对应的csv文件名
        csv_list = []
        for csv_id in csv_ids:
            csv_info = CsvInfo.query.get(csv_id)
            if csv_info:
                csv_list.append({
                    "label": csv_info.file_name,
                    "value": csv_id
                })

        # 构造返回的 classifications 结构
        classifications = [
            {
                "children": docx_list,
                "label": "选择docx文件",
                "value": "选择docx文件"
            },
            {
                "children": csv_list,
                "label": "选择csv文件",
                "value": "选择csv文件"
            }
        ]

        response = OrderedDict([
            ("code", 0),
            ("data", {
                "classifications": classifications,
                "message": "success"
            }),
            ("errMsg", "success")
        ])
        return jsonify(response), 200

    except Exception as e:
        response = OrderedDict([
            ("code", 0),
            ("data", {
                "classifications": []
            }),
            ("errMsg", f"发生错误: {str(e)}")
        ])
        return jsonify(response), 500


# 首页
# 02. 查询节点或关系的详细信息
@app.route('/details', methods=['POST'])
def get_details():
    data = request.json
    element_type = data.get("type")  # 期望的值为 'node' 或 'relationship'
    element_id = data.get("id")  # 传入的节点或关系的 ID

    if not element_type or not element_id:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请提供节点或关系类型以及ID"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

    try:
        element_id = int(element_id)  # 确保 element_id 为整数
        if element_type == "node":
            details = neo4j_connector.get_node_details(element_id)
        elif element_type == "relationship":
            details = neo4j_connector.get_relationship_details(element_id)
        else:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": "无效的类型，必须为 'node' 或 'relationship'"
                })
            ])
            return Response(json.dumps(response), mimetype='application/json')

        if not details:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": f"未找到ID为 {element_id} 的{element_type}"
                })
            ])
            return Response(json.dumps(response), mimetype='application/json')

        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "success",
                "details": details
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    except Exception as e:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"发生错误: {str(e)}"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    finally:
        neo4j_connector.close()


# 首页
# 03. 高级查询接口
@app.route('/advanced_query', methods=['POST'])
def advanced_query():
    data = request.json
    cypher_query = data.get("query", "")

    if not cypher_query:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "请提供Cypher查询语句"
            }
        }
        return jsonify(response), 400

    neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

    try:
        with neo4j_connector.driver.session() as session:
            # 执行原始查询获取节点
            result = session.run(cypher_query)
            nodes = []
            node_ids = set()  # 用于存储已经获取到的节点ID

            for record in result:
                for value in record.values():
                    if isinstance(value, Node):
                        node = {
                            "id": value.id,
                            "name": value.get("name"),
                            "major_classification": value.get("major_classification"),
                            "minor_classification": value.get("minor_classification"),
                            "type": value.get("type"),
                            "docx_id": value.get("docx_id"),
                            "csv_id": value.get("csv_id"),
                            "elementId": value.element_id,
                            "color_id": neo4j_connector.get_color_id(value.get("type"))  # 获取 color_id
                        }
                        nodes.append(node)
                        node_ids.add(value.id)  # 记录节点ID

            # 如果没有查询到节点，则直接返回
            if not node_ids:
                response_data = {
                    "message": "No nodes found",
                    "nodes": [],
                    "relationships": [],
                    "other_results": []
                }
                return jsonify({
                    "code": 0,
                    "errMsg": "success",
                    "data": response_data
                }), 200

            # 查询这些节点之间的关系
            relationship_query = f"""
            MATCH (n)-[r]->(m)
            WHERE id(n) IN {list(node_ids)} AND id(m) IN {list(node_ids)}
            RETURN r
            """
            relationship_result = session.run(relationship_query)
            relationships = []

            for record in relationship_result:
                rel = record['r']
                relationships.append({
                    "relationship_id": rel.id,
                    "from": rel.start_node.id,
                    "to": rel.end_node.id,
                    "text": rel.get("name"),
                    "type": rel.get("type"),
                    "docx_id": rel.get("docx_id"),
                    "csv_id": rel.get("csv_id"),
                    "elementId": rel.element_id
                })

            # 去重节点和关系
            nodes = {node['id']: node for node in nodes}.values()
            relationships = {rel['relationship_id']: rel for rel in relationships}.values()

            response_data = {
                "message": "success",
                "nodes": list(nodes),
                "relationships": list(relationships),
                "other_results": []  # 如果没有非节点和关系的结果
            }

            response = {
                "code": 0,
                "errMsg": "success",
                "data": response_data
            }
            return jsonify(response), 200

    except Exception as e:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"执行查询时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500

    finally:
        neo4j_connector.close()


# 首页
# 04. 删除关系接口，但不会删除节点
@app.route('/delete_relationship', methods=['POST'])
def delete_relationship():
    data = request.json
    relationship_id = data.get("relationship_id")

    if not relationship_id:
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "请提供需要删除的关系ID"
            })
        ])
        return jsonify(response), 400

    neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

    try:
        success = neo4j_connector.delete_relationship_by_id(relationship_id)
        neo4j_connector.close()

        if success:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": "关系删除成功",
                    "relationship_id": relationship_id
                })
            ])
            return jsonify(response), 200
        else:
            response = OrderedDict([
                ("code", 0),
                ("errMsg", "success"),
                ("data", {
                    "message": f"未找到ID为 {relationship_id} 的关系"
                })
            ])
            return jsonify(response), 404

    except Exception as e:
        neo4j_connector.close()
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": f"删除关系时发生错误: {str(e)}"
            })
        ])
        return jsonify(response), 500


# 首页
# 05. 新增已存在的两个实体之间的关系
@app.route('/create_relationship', methods=['POST'])
def create_relationship():
    data = request.json
    start_node_id = data.get("start_node_id")
    end_node_id = data.get("end_node_id")
    relationship_type = data.get("relationship_type")

    # 检查是否缺少必要参数
    if not start_node_id or not end_node_id or not relationship_type:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "缺少必要的参数"
            }
        }
        return jsonify(response), 400

    try:
        neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

        # 检查头实体和尾实体是否存在
        if not neo4j_connector.node_exists(start_node_id):
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": f"头实体ID {start_node_id} 不存在"
                }
            }
            return jsonify(response), 404

        if not neo4j_connector.node_exists(end_node_id):
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": f"尾实体ID {end_node_id} 不存在"
                }
            }
            return jsonify(response), 404

        relationship_id = neo4j_connector.create_relationship(start_node_id, end_node_id, relationship_type)

        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "关系创建成功",
                "relationship_id": relationship_id
            }
        }
        return jsonify(response), 200

    except Exception as e:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": f"创建关系时发生错误: {str(e)}"
            }
        }
        return jsonify(response), 500
    finally:
        neo4j_connector.close()



@app.route('/get_relationship_classifications', methods=['GET'])
def get_relationship_classifications():
    relationship_id = request.args.get('relationship_id')

    if not relationship_id:
        response = {
            "code": 0,
            "errMsg": "success",
            "data": {
                "message": "缺少必要的参数：relationship_id"
            }
        }
        return jsonify(response), 400

    neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")

    try:
        start_node_classification, end_node_classification = neo4j_connector.get_classifications_by_relationship(
            relationship_id)

        if start_node_classification and end_node_classification:
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": "success",
                    "relationship_id": relationship_id,
                    "start_node_classification": start_node_classification,
                    "end_node_classification": end_node_classification
                }
            }
            return jsonify(response), 200
        else:
            response = {
                "code": 0,
                "errMsg": "success",
                "data": {
                    "message": "未找到对应的关系或节点信息"
                }
            }
            return jsonify(response), 404

    except Exception as e:
        response = {
            "code": 1,
            "errMsg": str(e),
            "data": {
                "message": "查询节点类型信息时发生错误"
            }
        }
        return jsonify(response), 500
    finally:
        neo4j_connector.close()



## V2 人工审核
@app.route('/review/entities', methods=['GET'])
def get_entities_for_review():
    try:
        entities = EntityReview.query.filter_by(status='待审核').all()
        result = [
            {
                'entity_name': entity.entity_name,
                'source_file': entity.source_file,
                'source_text': entity.source_text,
                'status': entity.status,
                'id': entity.id  # 前端需要用到的唯一标识符
            }
            for entity in entities
        ]
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/review/submit', methods=['POST'])
def submit_review():
    data = request.json
    entity_id = data.get('id')
    action = data.get('action')
    merge_to = data.get('merge_to', None)

    try:
        entity = EntityReview.query.get(entity_id)
        if not entity:
            return jsonify({'error': 'Entity not found'}), 404

        if action == 'merge':
            entity.status = '已合并'
            entity.review_action = 'merge'
            entity.merge_to_entity = merge_to
            # 调用 Neo4jConnector 合并实体
            neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")
            neo4j_connector.merge_entity(entity.entity_name, merge_to)
            neo4j_connector.close()
        elif action == 'add':
            entity.status = '已添加'
            entity.review_action = 'add'
            # 调用 Neo4jConnector 新增实体
            neo4j_connector = Neo4jConnector("bolt://localhost:7687", "neo4j", "12345678")
            neo4j_connector.add_entity_to_neo4j(entity.entity_name, entity.entity_type)
            neo4j_connector.close()
        elif action == 'reject':
            entity.status = '已拒绝'
            entity.review_action = 'reject'

        db.session.commit()
        return jsonify({'message': 'Review submitted successfully'}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500



#####  可视化建模
@app.route('/save_visualization', methods=['POST'])
def save_visualization():
    try:
        # 获取请求中的 JSON 数据
        data = request.get_json()
        nodes = data.get('nodes', [])
        relationships = data.get('relationships', [])

        # 保存 nodes 到 entity_classifications 表
        for node in nodes:
            entity_type = node.get('text')
            if entity_type:  # 确保类型名不为空
                # 检查类型是否已存在
                existing_type = EntityClassification.query.filter_by(entity_types=entity_type).first()
                if not existing_type:
                    new_entity_type = EntityClassification(entity_types=entity_type)
                    new_entity_type.created_at = datetime.utcnow()  # 直接设置 created_at 属性
                    db.session.add(new_entity_type)

        # 保存 relationships 到 relationship_models 表
        for relationship in relationships:
            relation_name = relationship.get('text')
            start_node_id = relationship.get('from')
            end_node_id = relationship.get('to')

            # 获取 start 和 end 的类型名称
            start_node_type = next((node['text'] for node in nodes if node['id'] == start_node_id), None)
            end_node_type = next((node['text'] for node in nodes if node['id'] == end_node_id), None)

            if relation_name and start_node_type and end_node_type:  # 确保所有信息都存在
                # 检查关系是否已存在
                existing_relation = RelationshipModel.query.filter_by(
                    relation_name=relation_name,
                    start_node_type=start_node_type,
                    end_node_type=end_node_type
                ).first()

                if not existing_relation:
                    new_relationship = RelationshipModel(
                        relation_name=relation_name,
                        start_node_type=start_node_type,
                        end_node_type=end_node_type
                    )
                    new_relationship.created_at = datetime.utcnow()  # 直接设置 created_at 属性
                    db.session.add(new_relationship)

        # 提交所有更改
        db.session.commit()

        # 返回成功响应
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "message": "数据已成功保存到数据库中"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    except Exception as e:
        # 返回错误响应
        response = OrderedDict([
            ("code", 1),
            ("errMsg", "error"),
            ("data", {
                "message": f"保存数据时发生错误: {str(e)}"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')


@app.route('/load_visualization', methods=['GET'])
def load_visualization():
    try:
        # 从 entity_classifications 表获取所有节点类型
        nodes = EntityClassification.query.all()
        nodes_data = [
            {
                "id": node.id,
                "text": node.entity_types,  # 将类型名称作为节点显示文本
                # 如果需要，可以添加其他字段，例如颜色等属性
            }
            for node in nodes
        ]

        # 从 relationship_models 表获取所有关系模型
        relationships = RelationshipModel.query.all()
        relationships_data = [
            {
                "relationship_id": relationship.id,
                "from": next((node.id for node in nodes if node.entity_types == relationship.start_node_type), None),
                "to": next((node.id for node in nodes if node.entity_types == relationship.end_node_type), None),
                "text": relationship.relation_name  # 关系名称
            }
            for relationship in relationships
        ]

        # 返回节点和关系数据
        response = OrderedDict([
            ("code", 0),
            ("errMsg", "success"),
            ("data", {
                "nodes": nodes_data,
                "relationships": relationships_data
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')

    except Exception as e:
        # 返回错误响应
        response = OrderedDict([
            ("code", 1),
            ("errMsg", "error"),
            ("data", {
                "message": f"加载数据时发生错误: {str(e)}"
            })
        ])
        return Response(json.dumps(response), mimetype='application/json')



if __name__ == '__main__':
    with app.app_context():
        reset_color_mapping()
        update_color_mapping()  # 在应用启动时初始化 ColorMapping 表
    app.run(host='0.0.0.0', port=5010, debug=True)
