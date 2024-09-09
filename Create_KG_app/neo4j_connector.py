from neo4j import GraphDatabase
from collections import OrderedDict
from models import db, ColorMapping


def preprocess_name(name):
    return name.replace('"', '\\"').replace('(', '\\(').replace(')', '\\)').replace("'", "\\'")


class Neo4jConnector:
    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def get_color_id(self, node_type):
        color_mapping = ColorMapping.query.filter_by(type=node_type).first()
        if color_mapping:
            return color_mapping.color_id
        else:
            return None


    def get_node_types(self):
        """获取所有独特的节点类型"""
        node_types = set()
        with self.driver.session() as session:
            result = session.run("MATCH (n:Entity) RETURN DISTINCT n.type AS type")
            node_types = {record["type"] for record in result if record["type"]}
        return node_types


    def find_node_and_relationships(self, node_name, docx_id=None, csv_id=None):
        with self.driver.session() as session:
            # 初步查询：获取当前节点及其直接关联的节点和关系
            primary_query = """
                MATCH (n:Entity {name: $name})-[r:RELATIONSHIP]-(related:Entity)
                WHERE ($docx_id IS NULL OR n.docx_id = $docx_id) 
                  AND ($csv_id IS NULL OR n.csv_id = $csv_id)
                RETURN DISTINCT 
                    id(n) AS node_id, n.name AS node_name, 
                    n.major_classification AS major_classification,
                    n.minor_classification AS minor_classification,
                    n.type AS node_type, n.docx_id AS node_docx_id,
                    n.csv_id AS node_csv_id, elementId(n) AS element_id,
                    id(related) AS related_node_id, related.name AS related_node_name,
                    related.major_classification AS related_major_classification,
                    related.minor_classification AS related_minor_classification,
                    related.type AS related_node_type, 
                    related.docx_id AS related_node_docx_id,
                    related.csv_id AS related_node_csv_id, 
                    elementId(related) AS related_element_id,
                    id(r) AS relationship_id, r.name AS relationship_name
            """
            params = {"name": node_name, "docx_id": docx_id, "csv_id": csv_id}
            result = session.run(primary_query, params)

            nodes = OrderedDict()
            relationships = set()
            node_data = None

            # 处理初步查询结果
            for record in result:
                if not node_data:
                    node_data = {
                        "id": record["node_id"],
                        "name": record["node_name"],
                        "major_classification": record["major_classification"],
                        "minor_classification": record["minor_classification"],
                        "type": record["node_type"] if record["node_type"] else "",
                        "docx_id": record["node_docx_id"] if record["node_docx_id"] else "",
                        "csv_id": record["node_csv_id"] if record["node_csv_id"] else "",
                        "elementId": record["element_id"],
                    }

                if record["related_node_id"] not in nodes:
                    nodes[record["related_node_id"]] = {
                        "id": record["related_node_id"],
                        "name": record["related_node_name"],
                        "major_classification": record["related_major_classification"],
                        "minor_classification": record["related_minor_classification"],
                        "type": record["related_node_type"] if record["related_node_type"] else "",
                        "docx_id": record["related_node_docx_id"] if record["related_node_docx_id"] else "",
                        "csv_id": record["related_node_csv_id"] if record["related_node_csv_id"] else "",
                        "elementId": record["related_element_id"],
                    }

                relationship_tuple = (
                    record["relationship_id"],
                    min(record["node_id"], record["related_node_id"]),
                    max(record["node_id"], record["related_node_id"]),
                    record["relationship_name"]
                )
                relationships.add(relationship_tuple)

            # 查询二级节点之间的关系
            secondary_query = """
                MATCH (a:Entity)-[r:RELATIONSHIP]-(b:Entity)
                WHERE id(a) IN $node_ids AND id(b) IN $node_ids
                RETURN DISTINCT 
                    id(r) AS relationship_id, r.name AS relationship_name,
                    id(a) AS from_node, id(b) AS to_node
            """
            node_ids = list(nodes.keys())
            secondary_result = session.run(secondary_query, {"node_ids": node_ids})

            # 处理二级关系
            for record in secondary_result:
                relationship_tuple = (
                    record["relationship_id"],
                    min(record["from_node"], record["to_node"]),
                    max(record["from_node"], record["to_node"]),
                    record["relationship_name"]
                )
                relationships.add(relationship_tuple)

            # 确保所有关系中的节点都在 nodes 中
            extra_node_ids = set()
            for rel in relationships:
                if rel[1] not in nodes:
                    extra_node_ids.add(rel[1])
                if rel[2] not in nodes:
                    extra_node_ids.add(rel[2])

            if extra_node_ids:
                extra_query = """
                    MATCH (n:Entity)
                    WHERE id(n) IN $node_ids
                    RETURN DISTINCT 
                        id(n) AS node_id, n.name AS node_name, 
                        n.major_classification AS major_classification,
                        n.minor_classification AS minor_classification,
                        n.type AS node_type, 
                        n.docx_id AS node_docx_id,
                        n.csv_id AS node_csv_id, 
                        elementId(n) AS element_id
                """
                extra_nodes_result = session.run(extra_query, {"node_ids": list(extra_node_ids)})

                for record in extra_nodes_result:
                    nodes[record["node_id"]] = {
                        "id": record["node_id"],
                        "name": record["node_name"],
                        "major_classification": record["major_classification"],
                        "minor_classification": record["minor_classification"],
                        "type": record["node_type"] if record["node_type"] else "",
                        "docx_id": record["node_docx_id"] if record["node_docx_id"] else "",
                        "csv_id": record["node_csv_id"] if record["node_csv_id"] else "",
                        "elementId": record["element_id"],
                    }

            # 格式化 relationships 为输出格式
            formatted_relationships = [
                {"relationship_id": rel[0], "from": rel[1], "to": rel[2], "text": rel[3]}
                for rel in relationships
            ]

            return node_data, list(nodes.values()), formatted_relationships

    def _process_secondary_results(self, results):
        nodes = OrderedDict()
        relationships = []

        for record in results:
            # 对于每个记录，进行 None 检查，避免出现 subscriptable 错误
            if record is None:
                continue

            node_color_id = self.get_color_id(record.get("node_type", ""))
            target_node_color_id = self.get_color_id(record.get("target_node_type", ""))

            if record.get("node_id") not in nodes:
                nodes[record["node_id"]] = {
                    "id": record["node_id"],
                    "name": record.get("node_name", ""),
                    "major_classification": record.get("major_classification", ""),
                    "minor_classification": record.get("minor_classification", ""),
                    "type": record.get("node_type", ""),
                    "docx_id": record.get("node_docx_id", ""),
                    "csv_id": record.get("node_csv_id", ""),
                    "color_id": node_color_id
                }

            if record.get("target_node_id") not in nodes:
                nodes[record["target_node_id"]] = {
                    "id": record["target_node_id"],
                    "name": record.get("target_node_name", ""),
                    "major_classification": record.get("target_major_classification", ""),
                    "minor_classification": record.get("target_minor_classification", ""),
                    "type": record.get("target_node_type", ""),
                    "docx_id": record.get("target_node_docx_id", ""),
                    "csv_id": record.get("target_node_csv_id", ""),
                    "color_id": target_node_color_id
                }

            relationships.append({
                "relationship_id": record.get("relationship_id"),
                "from": record.get("node_id"),
                "to": record.get("target_node_id"),
                "text": record.get("relationship_name", ""),
                "type": record.get("relationship_type", ""),
                "docx_id": record.get("relationship_docx_id", ""),
                "csv_id": record.get("relationship_csv_id", "")
            })

        return list(nodes.values()), relationships

    def _process_results(self, result, query_type):
        node_data = None
        related_nodes = OrderedDict()
        relationships = []

        for record in result:
            # 安全地获取字段值，防止字段缺失引发的 KeyError
            node_color_id = self.get_color_id(record.get("node_type", ""))
            target_node_color_id = self.get_color_id(record.get("target_node_type", ""))

            if query_type == "head":
                if not node_data:
                    node_data = {
                        "id": record.get("node_id"),
                        "name": record.get("node_name", ""),
                        "major_classification": record.get("major_classification", ""),
                        "minor_classification": record.get("minor_classification", ""),
                        "type": record.get("node_type", ""),
                        "docx_id": record.get("node_docx_id", ""),
                        "csv_id": record.get("node_csv_id", ""),
                        "color_id": node_color_id
                    }

                if record.get("target_node_id") not in related_nodes:
                    related_nodes[record.get("target_node_id")] = {
                        "id": record.get("target_node_id"),
                        "name": record.get("target_node_name", ""),
                        "major_classification": record.get("target_major_classification", ""),
                        "minor_classification": record.get("target_minor_classification", ""),
                        "type": record.get("target_node_type", ""),
                        "docx_id": record.get("target_docx_id", ""),
                        "csv_id": record.get("target_csv_id", ""),
                        "color_id": target_node_color_id
                    }

                relationships.append({
                    "relationship_id": record.get("relationship_id"),
                    "from": record.get("node_id"),
                    "to": record.get("target_node_id"),
                    "text": record.get("relationship_name", ""),
                    "type": record.get("relationship_type", ""),
                    "docx_id": record.get("relationship_docx_id", ""),
                    "csv_id": record.get("relationship_csv_id", "")
                })

            elif query_type == "tail":
                if not node_data:
                    node_data = {
                        "id": record.get("target_node_id"),
                        "name": record.get("target_node_name", ""),
                        "major_classification": record.get("target_major_classification", ""),
                        "minor_classification": record.get("target_minor_classification", ""),
                        "type": record.get("target_node_type", ""),
                        "docx_id": record.get("target_docx_id", ""),
                        "csv_id": record.get("target_csv_id", ""),
                        "color_id": target_node_color_id
                    }

                if record.get("node_id") not in related_nodes:
                    related_nodes[record.get("node_id")] = {
                        "id": record.get("node_id"),
                        "name": record.get("node_name", ""),
                        "major_classification": record.get("major_classification", ""),
                        "minor_classification": record.get("minor_classification", ""),
                        "type": record.get("node_type", ""),
                        "docx_id": record.get("node_docx_id", ""),
                        "csv_id": record.get("node_csv_id", ""),
                        "color_id": node_color_id
                    }

                relationships.append({
                    "relationship_id": record.get("relationship_id"),
                    "from": record.get("node_id"),
                    "to": record.get("target_node_id"),
                    "text": record.get("relationship_name", ""),
                    "type": record.get("relationship_type", ""),
                    "docx_id": record.get("relationship_docx_id", ""),
                    "csv_id": record.get("relationship_csv_id", "")
                })

        return node_data, list(related_nodes.values()), relationships

    def get_node_by_id(self, node_id):
        # 查询特定ID的节点
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n:Entity)
                WHERE id(n) = $id
                RETURN id(n) as id, n.name as name, n.major_classification as major_classification, 
                       n.minor_classification as minor_classification, n.type as type, 
                       n.docx_id as docx_id, n.csv_id as csv_id, elementId(n) as element_id
                """,
                id=int(node_id)
            )

            node_details = result.single()
            if node_details:
                color_id = self.get_color_id(node_details["type"])
                return {
                    "id": node_details["id"],
                    "name": node_details["name"],
                    "major_classification": node_details["major_classification"],
                    "minor_classification": node_details["minor_classification"],
                    "type": node_details["type"],
                    "docx_id": node_details["docx_id"],
                    "csv_id": node_details["csv_id"],
                    "elementId": node_details["element_id"],
                    "color_id": color_id
                }
            else:
                return None

    def find_all_nodes_and_relationships(self):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n:Entity)-[r]->(m:Entity)
                RETURN toInteger(id(n)) AS node_id, n.name AS node_name, 
                       n.major_classification AS major_classification, 
                       n.minor_classification AS minor_classification, 
                       n.type AS type, 
                       n.docx_id AS docx_id, 
                       n.csv_id AS csv_id, 
                       elementId(n) AS element_id,
                       toInteger(id(r)) AS relationship_id, r.name AS relationship_name, 
                       toInteger(id(m)) AS target_node_id, m.name AS target_node_name, 
                       m.major_classification AS target_major_classification, 
                       m.minor_classification AS target_minor_classification, 
                       m.type AS target_type, 
                       m.docx_id AS target_docx_id, 
                       m.csv_id AS target_csv_id, 
                       elementId(m) AS target_element_id
                """
            )

            nodes = OrderedDict()
            relationships = []

            for record in result:
                node_color_id = self.get_color_id(record["type"])
                target_node_color_id = self.get_color_id(record["target_type"])

                if record["node_id"] not in nodes:
                    nodes[record["node_id"]] = {
                        "id": record["node_id"],
                        "name": record["node_name"],
                        "major_classification": record["major_classification"],
                        "minor_classification": record["minor_classification"],
                        "type": record["type"] if record["type"] else "",
                        "docx_id": record["docx_id"] if record["docx_id"] else "",
                        "csv_id": record["csv_id"] if record["csv_id"] else "",
                        "elementId": record["element_id"],
                        "color_id": node_color_id  # 添加 color_id
                    }

                if record["target_node_id"] not in nodes:
                    nodes[record["target_node_id"]] = {
                        "id": record["target_node_id"],
                        "name": record["target_node_name"],
                        "major_classification": record["target_major_classification"],
                        "minor_classification": record["minor_classification"],
                        "type": record["target_type"] if record["target_type"] else "",
                        "docx_id": record["target_docx_id"] if record["target_docx_id"] else "",
                        "csv_id": record["target_csv_id"] if record["target_csv_id"] else "",
                        "elementId": record["target_element_id"],
                        "color_id": target_node_color_id  # 添加 color_id
                    }

                relationships.append({
                    "relationship_id": record["relationship_id"],
                    "from": record["node_id"],
                    "to": record["target_node_id"],
                    "text": record["relationship_name"]
                })

            return list(nodes.values()), relationships

    def get_all_docx_ids(self):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH ()-[r:RELATIONSHIP]->()
                WHERE r.docx_id IS NOT NULL
                RETURN DISTINCT r.docx_id AS docx_id
                """
            )
            docx_ids = [record["docx_id"] for record in result]
            return docx_ids

    def get_all_csv_ids(self):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH ()-[r:RELATIONSHIP]->()
                WHERE r.csv_id IS NOT NULL
                RETURN DISTINCT r.csv_id AS csv_id
                """
            )
            csv_ids = [record["csv_id"] for record in result]
            return csv_ids

    def find_nodes_and_relationships_by_docx_or_csv(self, docx_id=None, csv_id=None):
        with self.driver.session() as session:
            query = """
                MATCH (n:Entity)-[r:RELATIONSHIP]->(m:Entity)
                WHERE 1=1
            """
            params = {}

            if docx_id is not None:
                query += " AND r.docx_id = $docx_id"
                params['docx_id'] = docx_id

            if csv_id is not None:
                query += " AND r.csv_id = $csv_id"
                params['csv_id'] = csv_id

            query += """
                RETURN toInteger(id(n)) AS node_id, n.name AS node_name, 
                       n.major_classification AS major_classification, 
                       n.minor_classification AS minor_classification, 
                       n.type AS node_type,
                       n.docx_id AS node_docx_id,
                       n.csv_id AS node_csv_id,
                       toInteger(id(r)) AS relationship_id, r.name AS relationship_name, 
                       r.type AS relationship_type,
                       r.docx_id AS relationship_docx_id,
                       r.csv_id AS relationship_csv_id,
                       toInteger(id(m)) AS target_node_id, m.name AS target_node_name, 
                       m.major_classification AS target_major_classification, 
                       m.minor_classification AS target_minor_classification,
                       m.type AS target_node_type,
                       m.docx_id AS target_node_docx_id,
                       m.csv_id AS target_node_csv_id
            """

            result = session.run(query, params)

            nodes = OrderedDict()
            relationships = []

            for record in result:
                node_color_id = self.get_color_id(record["node_type"])
                target_node_color_id = self.get_color_id(record["target_node_type"])

                if record["node_id"] not in nodes:
                    nodes[record["node_id"]] = {
                        "id": record["node_id"],
                        "name": record["node_name"],
                        "major_classification": record["major_classification"],
                        "minor_classification": record["minor_classification"],
                        "type": record["node_type"],
                        "docx_id": record["node_docx_id"],
                        "csv_id": record["node_csv_id"],
                        "color_id": node_color_id  # 添加 color_id
                    }

                if record["target_node_id"] not in nodes:
                    nodes[record["target_node_id"]] = {
                        "id": record["target_node_id"],
                        "name": record["target_node_name"],
                        "major_classification": record["target_major_classification"],
                        "minor_classification": record["minor_classification"],
                        "type": record["target_node_type"],
                        "docx_id": record["target_node_docx_id"],
                        "csv_id": record["target_node_csv_id"],
                        "color_id": target_node_color_id  # 添加 color_id
                    }

                relationships.append({
                    "relationship_id": record["relationship_id"],
                    "from": record["node_id"],
                    "to": record["target_node_id"],
                    "text": record["relationship_name"],
                    "type": record["relationship_type"],
                    "docx_id": record["relationship_docx_id"],
                    "csv_id": record["relationship_csv_id"]
                })

            return list(nodes.values()), relationships

    def get_node_details(self, node_id):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n)
                WHERE id(n) = $id
                RETURN id(n) as id, 
                       n.name as name, 
                       n.major_classification as major_classification, 
                       n.minor_classification as minor_classification, 
                       n.type as type,
                       n.docx_id as docx_id,
                       n.csv_id as csv_id,
                       elementId(n) as elementId
                """,
                id=int(node_id)
            )

            node_details = result.single()
            if node_details:
                return dict(node_details)
            else:
                return None

    def get_relationship_details(self, relationship_id):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH ()-[r]->()
                WHERE id(r) = $relationship_id
                RETURN id(r) AS relationship_id, 
                       type(r) AS type, 
                       r.name AS name, 
                       r.docx_id AS docx_id, 
                       r.csv_id AS csv_id, 
                       elementId(r) AS element_id
                """,
                relationship_id=relationship_id
            )

            record = result.single()
            if record:
                details = {
                    "id": record["relationship_id"],
                    "type": record["type"] if record["type"] else "",
                    "name": record["name"] if record["name"] else "RELATIONSHIP",
                    "docx_id": record["docx_id"],
                    "csv_id": record["csv_id"],
                    "elementId": record["element_id"]
                }
                return details
            else:
                return None

    def update_node_name(self, node_id, new_name):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n)
                WHERE id(n) = $node_id
                SET n.name = $new_name
                RETURN n
                """,
                node_id=node_id, new_name=new_name
            )
            return result.single() is not None

    def update_relationship_name(self, relationship_id, new_name):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH ()-[r]->()
                WHERE id(r) = $relationship_id
                SET r.name = $new_name
                RETURN r
                """,
                relationship_id=relationship_id, new_name=new_name
            )
            return result.single() is not None

    def find_all_relationships(self):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (a:Entity)-[r]->(b:Entity)
                RETURN r, a, b
                """
            )

            relationships = []

            for record in result:
                from_major = record['a'].get('major_classification', '').strip()
                from_minor = record['a'].get('minor_classification', '').strip()
                to_major = record['b'].get('major_classification', '').strip()
                to_minor = record['b'].get('minor_classification', '').strip()

                from_classification = f"{from_major}-{from_minor}" if from_major and from_minor else from_major or from_minor
                to_classification = f"{to_major}-{to_minor}" if to_major and to_minor else to_major or to_minor

                relationship_data = {
                    "relationship_id": record['r'].id,
                    "relationship_name": record['r']['name'],
                    "from": {
                        "id": record['a'].id,
                        "name": record['a']['name'],
                        "classification": from_classification
                    },
                    "to": {
                        "id": record['b'].id,
                        "name": record['b']['name'],
                        "classification": to_classification
                    }
                }
                relationships.append(relationship_data)

            return relationships

    def get_all_classifications(self):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (n:Entity)
                RETURN DISTINCT n.major_classification AS major_classification, n.minor_classification AS minor_classification
            """)

            classifications = {}

            for record in result:
                major_class = record["major_classification"]
                minor_class = record["minor_classification"]

                if major_class not in classifications:
                    classifications[major_class] = []

                if minor_class:
                    classifications[major_class].append({"value": minor_class, "label": minor_class})

            # 将 classifications 转换为所需的格式
            classification_list = []
            for major_class, minor_classes in classifications.items():
                classification_list.append({
                    "value": major_class,
                    "label": major_class,
                    "children": minor_classes
                })

            return classification_list


    def filter_graph_by_criteria(self, start_node_type, end_node_type, start_node_name, end_node_name, relationship):
        query = """
            MATCH (start:Entity)-[r:RELATIONSHIP]->(end:Entity)
        """

        where_clauses = []
        params = {}

        if start_node_type:
            if start_node_type.get("major_classification"):
                where_clauses.append("start.major_classification = $start_major_classification")
                params["start_major_classification"] = start_node_type["major_classification"]

            if start_node_type.get("minor_classification"):
                where_clauses.append("start.minor_classification = $start_minor_classification")
                params["start_minor_classification"] = start_node_type["minor_classification"]

        if end_node_type:
            if end_node_type.get("major_classification"):
                where_clauses.append("end.major_classification = $end_major_classification")
                params["end_major_classification"] = end_node_type["major_classification"]

            if end_node_type.get("minor_classification"):
                where_clauses.append("end.minor_classification = $end_minor_classification")
                params["end_minor_classification"] = end_node_type["minor_classification"]

        if start_node_name:
            where_clauses.append("start.name CONTAINS $start_node_name")
            params["start_node_name"] = start_node_name

        if end_node_name:
            where_clauses.append("end.name CONTAINS $end_node_name")
            params["end_node_name"] = end_node_name

        if relationship:
            where_clauses.append("r.name CONTAINS $relationship")
            params["relationship"] = relationship

        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)

        # 增加排序逻辑
        query += " RETURN start, r, end ORDER BY r.created_at DESC"

        with self.driver.session() as session:
            result = session.run(query, params)
            filtered_data = []

            for record in result:
                # print(f"Retrieved record: {record}")  # Debugging line
                filtered_data.append({
                    "id": record["r"].id,  # 使用 Neo4j 的关系 ID 并将其重命名为 'id'
                    "start_node": record["start"]["name"],
                    "relationship": record["r"]["name"],
                    "end_node": record["end"]["name"]
                })

            return filtered_data

    def delete_node_and_relationships(self, node_id):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n:Entity)-[r]-()
                WHERE ID(n) = $node_id
                DELETE r, n
                RETURN COUNT(n) AS deleted_count
                """, {"node_id": node_id}
            )
            record = result.single()
            return record["deleted_count"] > 0

    def delete_relationship_by_id(self, relationship_id):
        query = """
        MATCH ()-[r]->()
        WHERE id(r) = $relationship_id
        DELETE r
        RETURN COUNT(r) AS deleted_count
        """
        with self.driver.session() as session:
            result = session.run(query, relationship_id=int(relationship_id))
            record = result.single()
            return record["deleted_count"] > 0


    def create_relationship(self, start_node_id, end_node_id, relationship_type):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (start:Entity), (end:Entity)
                WHERE id(start) = $start_node_id AND id(end) = $end_node_id
                CREATE (start)-[r:RELATIONSHIP {name: $relationship_type}]->(end)
                RETURN id(r) AS relationship_id
                """,
                start_node_id=start_node_id,
                end_node_id=end_node_id,
                relationship_type=relationship_type
            )
            relationship_id = result.single()["relationship_id"]
            return relationship_id

    def node_exists(self, node_id):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n)
                WHERE id(n) = $node_id
                RETURN n
                """,
                node_id=node_id
            )
            return result.single() is not None


    def get_all_classifications_with_entities(self):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (n:Entity)
                RETURN DISTINCT n.major_classification AS major_classification, 
                                n.minor_classification AS minor_classification, 
                                n.name AS entity_name,
                                id(n) AS entity_id
            """)

            classifications = {}

            for record in result:
                major_class = record["major_classification"]
                minor_class = record["minor_classification"]
                entity_name = record["entity_name"]
                entity_id = record["entity_id"]

                if major_class not in classifications:
                    classifications[major_class] = {}

                if minor_class not in classifications[major_class]:
                    classifications[major_class][minor_class] = []

                classifications[major_class][minor_class].append({
                    "label": entity_name,
                    "value": entity_id
                })

            classification_list = []
            for major_class, minor_classes in classifications.items():
                children = []
                for minor_class, entities in minor_classes.items():
                    children.append({
                        "label": minor_class,
                        "value": minor_class,
                        "children": entities
                    })
                classification_list.append({
                    "label": major_class,
                    "value": major_class,
                    "children": children
                })

            return classification_list


    def update_relationship_and_nodes(self, relationship_id, new_relationship_name=None, start_node_classification=None,
                                      end_node_classification=None):
        with self.driver.session() as session:
            query = """
                MATCH (start)-[r:RELATIONSHIP]->(end)
                WHERE id(r) = $relationship_id
            """

            # 根据提供的参数动态构建 SET 语句
            set_clauses = []
            params = {"relationship_id": relationship_id}

            if new_relationship_name:
                set_clauses.append("r.name = $new_relationship_name")
                params["new_relationship_name"] = new_relationship_name

            if start_node_classification:
                set_clauses.append("start.major_classification = $start_node_classification")
                params["start_node_classification"] = start_node_classification

            if end_node_classification:
                set_clauses.append("end.major_classification = $end_node_classification")
                params["end_node_classification"] = end_node_classification

            if set_clauses:
                query += " SET " + ", ".join(set_clauses)

            query += " RETURN r"

            result = session.run(query, params)
            return result.single() is not None


    def get_classifications_by_relationship(self, relationship_id):
        with self.driver.session() as session:
            query = """
                MATCH (start)-[r:RELATIONSHIP]->(end)
                WHERE id(r) = $relationship_id
                RETURN start.major_classification AS start_major_classification,
                       start.minor_classification AS start_minor_classification,
                       end.major_classification AS end_major_classification,
                       end.minor_classification AS end_minor_classification
            """
            result = session.run(query, {"relationship_id": int(relationship_id)})

            record = result.single()
            if record:
                start_classification = f"{record['start_major_classification']}-{record['start_minor_classification']}"
                end_classification = f"{record['end_major_classification']}-{record['end_minor_classification']}"
                return start_classification, end_classification
            else:
                return None, None


    def update_relation_by_id(self, relation_id, new_start_node_name, new_relationship_name, new_end_node_name):
        with self.driver.session() as session:
            query = (
                "MATCH (start)-[r]->(end) "
                "WHERE ID(r) = $relation_id "
                "SET start.name = $new_start_node_name, "
                "    r.name = $new_relationship_name, "
                "    end.name = $new_end_node_name "
                "RETURN start.name AS start_node, r.name AS relationship, end.name AS end_node"
            )
            result = session.run(
                query,
                relation_id=relation_id,
                new_start_node_name=new_start_node_name,
                new_relationship_name=new_relationship_name,
                new_end_node_name=new_end_node_name
            )

            record = result.single()
            if record:
                print(f"Updated record: {record}")  # Debugging line
                return {
                    "start_node": record["start_node"],
                    "relationship": record["relationship"],
                    "end_node": record["end_node"]
                }
            else:
                print("No record updated")  # Debugging line
                return None

    def find_relation_by_id(self, relation_id):
        """根据关系ID查找关系信息"""
        with self.driver.session() as session:
            query = (
                "MATCH (start)-[r]->(end) "
                "WHERE ID(r) = $relation_id "
                "RETURN start.name AS start_node, r.name AS relationship, end.name AS end_node"
            )
            result = session.run(query, relation_id=relation_id)

            # 提取结果
            record = result.single()
            if record:
                return {
                    "id": relation_id,
                    "start_node": record["start_node"],
                    "relationship": record["relationship"],
                    "end_node": record["end_node"]
                }
            else:
                return None

    def find_node_name_by_id(self, node_id):
        """根据节点ID查找节点名称"""
        with self.driver.session() as session:
            query = (
                "MATCH (n) "
                "WHERE ID(n) = $node_id "
                "RETURN n.name AS name"
            )
            result = session.run(query, node_id=node_id)

            # 提取结果
            record = result.single()
            if record:
                return record["name"]
            else:
                return None

    def get_node_name_by_id(self, node_id):
        """根据节点ID获取节点名称"""
        with self.driver.session() as session:
            query = "MATCH (n) WHERE ID(n) = $node_id RETURN n.name AS name"
            result = session.run(query, node_id=node_id)
            record = result.single()
            return record["name"] if record else None

    def get_relationship_name_by_id(self, relationship_id):
        with self.driver.session() as session:
            result = session.run(
                "MATCH ()-[r:RELATIONSHIP]->() WHERE id(r) = $relationship_id RETURN r.name AS name",
                relationship_id=relationship_id
            )
            record = result.single()
            return record["name"] if record else None


    def update_relationship(self, relation_id, start_node_name=None, relationship_name=None, end_node_name=None):
        with self.driver.session() as session:
            query = """
                MATCH (start)-[r:RELATIONSHIP]->(end)
                WHERE id(r) = $relation_id
                """
            set_clauses = []
            params = {"relation_id": relation_id}

            if start_node_name:
                set_clauses.append("SET start.name = $start_node_name")
                params["start_node_name"] = start_node_name

            if relationship_name:
                set_clauses.append("SET r.name = $relationship_name")
                params["relationship_name"] = relationship_name

            if end_node_name:
                set_clauses.append("SET end.name = $end_node_name")
                params["end_node_name"] = end_node_name

            # 拼接 SET 子句到查询
            if set_clauses:
                query += " ".join(set_clauses)

            session.run(query, params)



    def create_srelationship(self, start_node_name, relationship_name, end_node_name):
        query = (
            "MERGE (a:Entity {name: $start_node_name}) "
            "MERGE (b:Entity {name: $end_node_name}) "
            "MERGE (a)-[r:RELATIONSHIP {name: $relationship_name}]->(b) "
            "RETURN a, r, b"
        )

        with self.driver.session() as session:
            session.run(query,
                        start_node_name=start_node_name,
                        end_node_name=end_node_name,
                        relationship_name=relationship_name)


    ## 人工审核
    def create_relationship2(self, head, relation, tail):
        """创建实体关系"""
        with self.driver.session() as session:
            session.write_transaction(self._create_and_return_relationship, head, relation, tail)

    @staticmethod
    def _create_and_return_relationship(tx, head, relation, tail):
        query = (
            "MERGE (h:Entity {name: $head}) "
            "MERGE (t:Entity {name: $tail}) "
            "MERGE (h)-[r:RELATIONSHIP {name: $relation}]->(t) "
            "RETURN h, r, t"
        )
        tx.run(query, head=head, tail=tail, relation=relation)

    def add_property_to_entity(self, entity_name, property_name, property_value):
        """将属性添加到实体"""
        with self.driver.session() as session:
            session.write_transaction(self._add_property_to_entity, entity_name, property_name, property_value)

    @staticmethod
    def _add_property_to_entity(tx, entity_name, property_name, property_value):
        query = (
            f"MERGE (e:Entity {{name: $entity_name}}) "
            f"SET e.`{property_name}` = $property_value "  # 使用反引号将属性名包裹以允许动态属性
            "RETURN e"
        )
        tx.run(query, entity_name=entity_name, property_value=property_value)

    def save_to_neo4j(self, csv_file_path):
        """根据三元组 CSV 文件的内容将数据存储到 Neo4j"""
        import pandas as pd

        df = pd.read_csv(csv_file_path)

        for index, row in df.iterrows():
            head = row['Head']
            relation = row['Relation']
            tail = row['Tail']
            type_ = row['Type']

            if type_ == '属性关系':
                # 如果是属性关系，将属性添加到实体
                self.add_property_to_entity(head, relation, tail)
            else:
                # 如果是实体关系，创建正常的三元组关系
                self.create_relationship2(head, relation, tail)

        print(f"Data from {csv_file_path} has been saved to Neo4j")



