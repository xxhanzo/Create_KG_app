import os
import traceback
from models import db, DocxInfo, CsvInfo, Entity, RelationshipModel, EntityReview
from docx import Document
from zhipuai import ZhipuAI
from neo4j import GraphDatabase
import pandas as pd
import re
import jieba
import json
from datetime import datetime
from celery_config import create_app, make_celery
from neo4j_connector import Neo4jConnector
from langchain.text_splitter import RecursiveCharacterTextSplitter
from knowledge_graph_builder import DocxProcessor, GLMProcessor, Neo4jSaver, TripleProcessor

api_key = 'e16456b694edfa6af99d45c5dfbb2048.YBFA139dJMO3SMBG'
com_key = 'c0828a92a058f312295e323ba41bccc5.dGOtblQOUy8UeSr8'

app = create_app()  # 创建 Flask 应用实例
db.init_app(app)  # 初始化数据库
celery = make_celery(app)  # 创建 Celery 实例


def save_filtered_prompts_to_json(filtered_prompts, output_file):
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(filtered_prompts, f, ensure_ascii=False, indent=4)



@celery.task
def save_knowledge_graph_from_csv(csv_id, file_path):
    try:
        with app.app_context():
            # 查找CSV文件记录
            csv_info = CsvInfo.query.get(csv_id)
            if not csv_info:
                print(f"未找到ID为 {csv_id} 的CSV文件记录")
                return

            # 更新状态为 'Generating'
            csv_info.status = 'Generating'
            db.session.commit()

            # 使用 Neo4jSaver 生成知识图谱
            neo4j_saver = Neo4jSaver("bolt://localhost:7687", "neo4j", "12345678")
            neo4j_saver.save_triples_to_neo4j(file_path, docx_id=None, csv_id=str(csv_id))  # 使用文件路径和CSV ID调用
            neo4j_saver.close()

            # 更新状态为 'Completed'
            csv_info.status = 'Completed'
            db.session.commit()
            print(f"知识图谱生成完成, CSV文件ID: {csv_id}")

    except Exception as e:
        with app.app_context():
            # 更新状态为 'Error'
            csv_info = CsvInfo.query.get(csv_id)
            if csv_info:
                csv_info.status = 'Error'
                db.session.commit()
        print(f"生成知识图谱时发生错误: {str(e)}")

@celery.task
def process_file(file_id, file_name):
    with app.app_context():
        file_record = None
        try:
            # 构建文件的绝对路径
            file_path = os.path.abspath(os.path.join('generate_data', file_name))
            print(f"Task started with file_id: {file_id}, file_path: {file_path}")

            # 检查文件是否存在
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Package not found at '{file_path}'")

            file_record = DocxInfo.query.get(file_id)
            if not file_record:
                print(f"No file record found with file_id: {file_id}")
                return

            print(f"File record found: {file_record.file_name}, updating status to 'Splitting Document'")
            file_record.status = 'Splitting_Document'
            db.session.commit()

            # 使用 DocxProcessor 进行文档拆分和处理
            processor = DocxProcessor(file_path)
            formatted_prompts = processor.format_for_chatglm()
            filtered_prompts, total_filtered = processor.filter_prompts_by_entities(formatted_prompts)

            # 保存 filtered_prompts
            output_folder = 'generate_data'
            if not os.path.exists(output_folder):
                os.makedirs(output_folder)

            output_prompt_file = os.path.join(output_folder, 'filtered_prompts.json')
            with open(output_prompt_file, 'w', encoding='utf-8') as f:
                json.dump(filtered_prompts, f, ensure_ascii=False, indent=4)
            print(f"Filtered prompts saved to {output_prompt_file}")

            # 更新状态为 'Generating Triples'
            file_record.status = 'Generating_Triples'
            db.session.commit()

            # 使用 GLMProcessor 生成三元组
            glm_processor = GLMProcessor(api_key, db.session)  # 使用顶部定义的 api_key
            responses = glm_processor.extract_triples(filtered_prompts)

            # 保存响应结果
            glm_responses_file = os.path.join(output_folder, f'{file_id}_responses.json')
            glm_processor.save_responses_to_file(glm_responses_file)
            print(f"GLM responses saved to {glm_responses_file}")

            # 更新状态为 'Processing Triples'
            file_record.status = 'Processing_Triples'
            db.session.commit()

            # 使用 TripleProcessor 进行三元组提取并保存为 CSV
            triple_processor = TripleProcessor()
            triples = triple_processor.extract_triples(responses)
            print(f"Number of extracted triples: {len(triples)}")

            # 生成三元组 CSV 文件的路径
            triples_csv_file = os.path.join(output_folder, f'{file_id}_triples.csv')
            triple_processor.save_triples_to_csv(triples, triples_csv_file)
            print(f"Triples saved to {triples_csv_file}")

            # 更新状态为 'Generating Knowledge Graph'
            file_record.status = 'Generating_KnowledgeGraph'
            db.session.commit()

            # 使用 Neo4jSaver 将三元组存储到 Neo4j
            neo4j_saver = Neo4jSaver("bolt://localhost:7687", "neo4j", "12345678")
            neo4j_saver.save_triples_to_neo4j(triples_csv_file, docx_id=str(file_id), csv_id=f"{file_id}_csv")
            neo4j_saver.close()
            print("Knowledge graph generated and stored in Neo4j")

            # 最终更新状态为 'Completed'
            file_record.status = 'Completed'
            db.session.commit()
            print(f"Task completed for file_id: {file_id}, status updated to 'Completed'")

        except Exception as e:
            print(f"An error occurred: {e}")
            traceback.print_exc()  # 输出详细的异常信息
            if file_record:
                file_record.status = 'Error'
                db.session.commit()
            raise e

