"""
SkillGraph — Skill & Career Relationship Explorer
====================================================
A single-file Flask application that uses CognoDB (a Neo4j-compatible
graph database, spoken to over the Bolt protocol via the official
Neo4j Python driver) to model and explore relationships between
People, Skills, Projects, Technologies and Job Roles.

Run:
    python app.py --seed     # loads/refreshes sample data (idempotent)
    python app.py            # starts the web server on :5000

Sections in this file:
    1. Imports
    2. Configuration
    3. Database setup (driver + query helper)
    4. Helper functions (serialization, error handling)
    5. Seed data + seeding routine
    6. API routes
    7. HTML/CSS/JS front end (single template, no Jinja needed)
    8. Application startup
"""

# ======================================================================
# 1. IMPORTS
# ======================================================================
import os
import sys
import logging
import argparse

from flask import Flask, jsonify, request, Response
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError, Neo4jError
from dotenv import load_dotenv

# ======================================================================
# 2. CONFIGURATION
# ======================================================================
load_dotenv()

COGNODB_URI = os.environ.get("COGNODB_URI", "")
COGNODB_USERNAME = os.environ.get("COGNODB_USERNAME", "")
COGNODB_PASSWORD = os.environ.get("COGNODB_PASSWORD", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("skillgraph")

app = Flask(__name__)


# ======================================================================
# 3. DATABASE SETUP
# ======================================================================
class Database:
    """Thin wrapper around the Neo4j driver so the rest of the app
    never has to think about sessions, connectivity checks or
    credential handling directly."""

    def __init__(self, uri, user, password):
        self._uri = uri
        self._user = user
        self._password = password
        self._driver = None

    def connect(self):
        """Create the driver and verify connectivity. Never raises to
        the caller — logs the problem and leaves self._driver as None
        so routes can fail gracefully with a friendly message."""
        if not self._uri or not self._user or not self._password:
            logger.error(
                "CognoDB credentials are missing. Set COGNODB_URI, "
                "COGNODB_USERNAME and COGNODB_PASSWORD in your .env file."
            )
            return
        try:
            self._driver = GraphDatabase.driver(
                self._uri, auth=(self._user, self._password)
            )
            self._driver.verify_connectivity()
            logger.info("Connected to CognoDB at %s", self._uri)
        except (ServiceUnavailable, AuthError, Neo4jError, ValueError) as exc:
            logger.error("Could not connect to CognoDB: %s", exc)
            self._driver = None

    def close(self):
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    @property
    def is_connected(self):
        return self._driver is not None

    def run(self, query, params=None):
        """Run a parameterized Cypher query and return a list of plain
        dicts. Raises DatabaseUnavailable if there is no live driver,
        which routes turn into a friendly 503 JSON response."""
        if self._driver is None:
            raise DatabaseUnavailable("No active connection to CognoDB.")
        try:
            with self._driver.session() as session:
                result = session.run(query, params or {})
                return [record.data() for record in result]
        except (ServiceUnavailable, AuthError) as exc:
            logger.error("Database error while running query: %s", exc)
            raise DatabaseUnavailable("Lost connection to CognoDB.") from exc
        except Neo4jError as exc:
            logger.error("Cypher error: %s", exc)
            raise


class DatabaseUnavailable(Exception):
    """Raised whenever we cannot reach CognoDB. Never shown to users
    with a stack trace — always converted into a friendly message."""
    pass


db = Database(COGNODB_URI, COGNODB_USERNAME, COGNODB_PASSWORD)
db.connect()


# ======================================================================
# 4. HELPER FUNCTIONS
# ======================================================================
def node_to_dict(node):
    """Convert a neo4j Node object (or a plain dict already) into a
    JSON-serializable dict, tagging it with its primary label."""
    if node is None:
        return None
    if isinstance(node, dict):
        return node
    data = dict(node)
    labels = list(node.labels)
    data["_type"] = labels[0] if labels else "Node"
    return data


def error_response(message, status=500):
    """Uniform error payload. Never includes stack traces."""
    return jsonify({"error": message}), status


def api_route(f):
    """Decorator that wraps a route so DatabaseUnavailable and any
    unexpected exception are caught, logged server-side, and turned
    into a friendly JSON error instead of a stack trace."""
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except DatabaseUnavailable:
            return error_response(
                "Unable to connect to SkillGraph database. Please try again later.",
                503,
            )
        except Neo4jError as exc:
            logger.error("Query failed in %s: %s", f.__name__, exc)
            return error_response("A database error occurred.", 500)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            logger.exception("Unexpected error in %s", f.__name__)
            return error_response("An unexpected error occurred.", 500)
    wrapper.__name__ = f.__name__
    return wrapper


def calc_match(person_skill_ids, role_required_skills):
    """Given the set of skill ids a person has and the list of skill
    nodes a role requires, compute matched skills, missing skills and
    a match percentage."""
    matched = [s for s in role_required_skills if s["id"] in person_skill_ids]
    missing = [s for s in role_required_skills if s["id"] not in person_skill_ids]
    total = len(role_required_skills)
    percentage = round((len(matched) / total) * 100) if total else 0
    return matched, missing, percentage


# ----------------------------------------------------------------------
# Static Cypher query maps.
#
# Every Cypher string below is a fixed, literal constant written out in
# full for each allowed label/field ahead of time — nothing is built
# with f-strings, .format(), % formatting or string concatenation at
# request time. Routes that used to interpolate a label or field name
# into a query now instead look up the *entire* pre-written query in
# one of these dicts using the label/field as a key. Any value that
# comes from the request (an id, a search term, a node type) is still
# validated against these same dict keys and then passed to the driver
# purely as a bound parameter ($id, $q) — never as part of the query
# text. This keeps 100% of Cypher text static while still supporting a
# small, fixed set of node types.
# ----------------------------------------------------------------------

# A. Dashboard statistics — one static "count nodes of this label"
# query per label (used by get_stats).
STATS_QUERIES = {
    "people": "MATCH (n:Person) RETURN count(n) AS c",
    "skills": "MATCH (n:Skill) RETURN count(n) AS c",
    "projects": "MATCH (n:Project) RETURN count(n) AS c",
    "technologies": "MATCH (n:Technology) RETURN count(n) AS c",
    "jobRoles": "MATCH (n:JobRole) RETURN count(n) AS c",
}

# H. Graph exploration — one static "node + its neighborhood" query
# per node type (used by get_graph_for_node). $id is a bound parameter.
GRAPH_NODE_QUERIES = {
    "Person": (
        "MATCH (n:Person {id: $id}) OPTIONAL MATCH (n)-[r]-(m) "
        "RETURN n, labels(n) AS nLabels, r, type(r) AS rType, m, labels(m) AS mLabels"
    ),
    "Skill": (
        "MATCH (n:Skill {id: $id}) OPTIONAL MATCH (n)-[r]-(m) "
        "RETURN n, labels(n) AS nLabels, r, type(r) AS rType, m, labels(m) AS mLabels"
    ),
    "Project": (
        "MATCH (n:Project {id: $id}) OPTIONAL MATCH (n)-[r]-(m) "
        "RETURN n, labels(n) AS nLabels, r, type(r) AS rType, m, labels(m) AS mLabels"
    ),
    "Technology": (
        "MATCH (n:Technology {id: $id}) OPTIONAL MATCH (n)-[r]-(m) "
        "RETURN n, labels(n) AS nLabels, r, type(r) AS rType, m, labels(m) AS mLabels"
    ),
    "JobRole": (
        "MATCH (n:JobRole {id: $id}) OPTIONAL MATCH (n)-[r]-(m) "
        "RETURN n, labels(n) AS nLabels, r, type(r) AS rType, m, labels(m) AS mLabels"
    ),
}

# Global search — one static "search this label by this field" query
# per label (used by search_all). $q is a bound parameter.
SEARCH_QUERIES = {
    "people": "MATCH (n:Person) WHERE toLower(n.name) CONTAINS toLower($q) RETURN n ORDER BY n.name LIMIT 8",
    "skills": "MATCH (n:Skill) WHERE toLower(n.name) CONTAINS toLower($q) RETURN n ORDER BY n.name LIMIT 8",
    "projects": "MATCH (n:Project) WHERE toLower(n.name) CONTAINS toLower($q) RETURN n ORDER BY n.name LIMIT 8",
    "technologies": "MATCH (n:Technology) WHERE toLower(n.name) CONTAINS toLower($q) RETURN n ORDER BY n.name LIMIT 8",
    "jobRoles": "MATCH (n:JobRole) WHERE toLower(n.title) CONTAINS toLower($q) RETURN n ORDER BY n.title LIMIT 8",
}


# ======================================================================
# 5. SEED DATA
# ======================================================================
PEOPLE_DATA = [
    {"id": "p1", "name": "Arjun Mehta", "email": "arjun.mehta@example.com", "experience": 3, "location": "Chennai"},
    {"id": "p2", "name": "Priya Sharma", "email": "priya.sharma@example.com", "experience": 2, "location": "Bangalore"},
    {"id": "p3", "name": "Rohan Kumar", "email": "rohan.kumar@example.com", "experience": 4, "location": "Hyderabad"},
    {"id": "p4", "name": "Sneha Iyer", "email": "sneha.iyer@example.com", "experience": 1, "location": "Coimbatore"},
    {"id": "p5", "name": "Karthik Raj", "email": "karthik.raj@example.com", "experience": 5, "location": "Chennai"},
    {"id": "p6", "name": "Ananya Nair", "email": "ananya.nair@example.com", "experience": 3, "location": "Kochi"},
    {"id": "p7", "name": "Vikram Singh", "email": "vikram.singh@example.com", "experience": 6, "location": "Bangalore"},
    {"id": "p8", "name": "Divya Menon", "email": "divya.menon@example.com", "experience": 2, "location": "Chennai"},
    {"id": "p9", "name": "Rahul Verma", "email": "rahul.verma@example.com", "experience": 4, "location": "Pune"},
    {"id": "p10", "name": "Meera Pillai", "email": "meera.pillai@example.com", "experience": 1, "location": "Chennai"},
    {"id": "p11", "name": "Aditya Rao", "email": "aditya.rao@example.com", "experience": 7, "location": "Mumbai"},
    {"id": "p12", "name": "Kavya Reddy", "email": "kavya.reddy@example.com", "experience": 3, "location": "Hyderabad"},
]

SKILLS_DATA = [
    {"id": "sk1", "name": "Python", "category": "Programming Language", "level": "Advanced"},
    {"id": "sk2", "name": "Java", "category": "Programming Language", "level": "Advanced"},
    {"id": "sk3", "name": "JavaScript", "category": "Programming Language", "level": "Advanced"},
    {"id": "sk4", "name": "TypeScript", "category": "Programming Language", "level": "Intermediate"},
    {"id": "sk5", "name": "React", "category": "Frontend", "level": "Advanced"},
    {"id": "sk6", "name": "Angular", "category": "Frontend", "level": "Intermediate"},
    {"id": "sk7", "name": "Vue.js", "category": "Frontend", "level": "Intermediate"},
    {"id": "sk8", "name": "Node.js", "category": "Backend", "level": "Advanced"},
    {"id": "sk9", "name": "Flask", "category": "Backend", "level": "Advanced"},
    {"id": "sk10", "name": "Django", "category": "Backend", "level": "Intermediate"},
    {"id": "sk11", "name": "SQL", "category": "Database", "level": "Advanced"},
    {"id": "sk12", "name": "MongoDB", "category": "Database", "level": "Intermediate"},
    {"id": "sk13", "name": "PostgreSQL", "category": "Database", "level": "Intermediate"},
    {"id": "sk14", "name": "REST API", "category": "Backend", "level": "Advanced"},
    {"id": "sk15", "name": "Git", "category": "Tooling", "level": "Advanced"},
    {"id": "sk16", "name": "Docker", "category": "DevOps", "level": "Intermediate"},
    {"id": "sk17", "name": "HTML", "category": "Frontend", "level": "Advanced"},
    {"id": "sk18", "name": "CSS", "category": "Frontend", "level": "Advanced"},
    {"id": "sk19", "name": "Machine Learning", "category": "Data Science", "level": "Intermediate"},
    {"id": "sk20", "name": "Data Structures", "category": "Computer Science", "level": "Advanced"},
    {"id": "sk21", "name": "AWS", "category": "Cloud", "level": "Intermediate"},
    {"id": "sk22", "name": "Firebase", "category": "Cloud", "level": "Intermediate"},
    {"id": "sk23", "name": "Kubernetes", "category": "DevOps", "level": "Beginner"},
    {"id": "sk24", "name": "GraphQL", "category": "Backend", "level": "Beginner"},
    {"id": "sk25", "name": "Redis", "category": "Database", "level": "Beginner"},
]

PROJECTS_DATA = [
    {"id": "pr1", "name": "E-Commerce Platform", "description": "A full online store with catalog, cart and checkout.", "difficulty": "Advanced"},
    {"id": "pr2", "name": "Task Management App", "description": "Kanban-style board for tracking team tasks.", "difficulty": "Intermediate"},
    {"id": "pr3", "name": "Chat Application", "description": "Real-time messaging app with rooms and presence.", "difficulty": "Advanced"},
    {"id": "pr4", "name": "Blog CMS", "description": "Content management system for publishing articles.", "difficulty": "Intermediate"},
    {"id": "pr5", "name": "Weather Dashboard", "description": "Live weather lookup with charts and forecasts.", "difficulty": "Beginner"},
    {"id": "pr6", "name": "Portfolio Website", "description": "Personal portfolio showcasing projects and skills.", "difficulty": "Beginner"},
    {"id": "pr7", "name": "Inventory System", "description": "Warehouse stock tracking with reporting.", "difficulty": "Advanced"},
    {"id": "pr8", "name": "Social Media Analytics", "description": "Dashboard analyzing engagement trends.", "difficulty": "Advanced"},
    {"id": "pr9", "name": "Recipe Finder App", "description": "Search recipes by ingredients on hand.", "difficulty": "Intermediate"},
    {"id": "pr10", "name": "Expense Tracker", "description": "Personal finance tracker with monthly summaries.", "difficulty": "Beginner"},
    {"id": "pr11", "name": "Video Streaming Service", "description": "Scalable service for uploading and streaming video.", "difficulty": "Advanced"},
    {"id": "pr12", "name": "Job Board Platform", "description": "Platform connecting job seekers and recruiters.", "difficulty": "Intermediate"},
]

TECH_DATA = [
    {"id": "t1", "name": "Python", "category": "Language"},
    {"id": "t2", "name": "JavaScript", "category": "Language"},
    {"id": "t3", "name": "TypeScript", "category": "Language"},
    {"id": "t4", "name": "React", "category": "Frontend"},
    {"id": "t5", "name": "Node.js", "category": "Backend"},
    {"id": "t6", "name": "Flask", "category": "Backend"},
    {"id": "t7", "name": "Django", "category": "Backend"},
    {"id": "t8", "name": "PostgreSQL", "category": "Database"},
    {"id": "t9", "name": "MongoDB", "category": "Database"},
    {"id": "t10", "name": "Docker", "category": "DevOps"},
    {"id": "t11", "name": "AWS", "category": "Cloud"},
    {"id": "t12", "name": "Firebase", "category": "Cloud"},
    {"id": "t13", "name": "Redis", "category": "Database"},
    {"id": "t14", "name": "GraphQL", "category": "Backend"},
    {"id": "t15", "name": "Kubernetes", "category": "DevOps"},
]

ROLES_DATA = [
    {"id": "r1", "title": "Full Stack Developer", "department": "Engineering", "experience_level": "Entry"},
    {"id": "r2", "title": "Python Developer", "department": "Engineering", "experience_level": "Entry"},
    {"id": "r3", "title": "Backend Developer", "department": "Engineering", "experience_level": "Mid"},
    {"id": "r4", "title": "Frontend Developer", "department": "Engineering", "experience_level": "Entry"},
    {"id": "r5", "title": "Software Engineer", "department": "Engineering", "experience_level": "Entry"},
    {"id": "r6", "title": "Data Analyst", "department": "Data", "experience_level": "Entry"},
    {"id": "r7", "title": "Data Engineer", "department": "Data", "experience_level": "Mid"},
    {"id": "r8", "title": "AI/ML Engineer", "department": "Data", "experience_level": "Mid"},
]

# (person_id, skill_id)
PERSON_SKILLS = [
    ("p1", "sk1"), ("p1", "sk9"), ("p1", "sk11"), ("p1", "sk15"), ("p1", "sk14"),
    ("p2", "sk3"), ("p2", "sk5"), ("p2", "sk17"), ("p2", "sk18"), ("p2", "sk15"),
    ("p3", "sk2"), ("p3", "sk20"), ("p3", "sk11"), ("p3", "sk15"), ("p3", "sk16"),
    ("p4", "sk1"), ("p4", "sk19"), ("p4", "sk11"), ("p4", "sk20"),
    ("p5", "sk8"), ("p5", "sk5"), ("p5", "sk12"), ("p5", "sk14"), ("p5", "sk16"), ("p5", "sk21"),
    ("p6", "sk1"), ("p6", "sk10"), ("p6", "sk13"), ("p6", "sk14"), ("p6", "sk15"),
    ("p7", "sk3"), ("p7", "sk6"), ("p7", "sk4"), ("p7", "sk17"), ("p7", "sk18"), ("p7", "sk22"),
    ("p8", "sk1"), ("p8", "sk9"), ("p8", "sk12"), ("p8", "sk15"),
    ("p9", "sk1"), ("p9", "sk21"), ("p9", "sk16"), ("p9", "sk23"), ("p9", "sk11"),
    ("p10", "sk3"), ("p10", "sk5"), ("p10", "sk8"), ("p10", "sk18"), ("p10", "sk15"),
    ("p11", "sk2"), ("p11", "sk11"), ("p11", "sk20"), ("p11", "sk16"), ("p11", "sk21"), ("p11", "sk15"),
    ("p12", "sk1"), ("p12", "sk19"), ("p12", "sk21"), ("p12", "sk11"),
]

# (person_id, project_id)
PERSON_PROJECTS = [
    ("p1", "pr1"), ("p1", "pr5"),
    ("p2", "pr2"), ("p2", "pr6"),
    ("p3", "pr7"),
    ("p4", "pr8"),
    ("p5", "pr1"), ("p5", "pr3"),
    ("p6", "pr4"),
    ("p7", "pr9"), ("p7", "pr6"),
    ("p8", "pr10"),
    ("p9", "pr11"),
    ("p10", "pr2"), ("p10", "pr12"),
    ("p11", "pr7"), ("p11", "pr11"),
    ("p12", "pr8"), ("p12", "pr12"),
]

# (project_id, technology_id)
PROJECT_TECH = [
    ("pr1", "t1"), ("pr1", "t6"), ("pr1", "t8"), ("pr1", "t10"),
    ("pr2", "t4"), ("pr2", "t5"), ("pr2", "t9"),
    ("pr3", "t5"), ("pr3", "t4"), ("pr3", "t13"),
    ("pr4", "t7"), ("pr4", "t8"), ("pr4", "t1"),
    ("pr5", "t2"), ("pr5", "t12"),
    ("pr6", "t4"), ("pr6", "t3"),
    ("pr7", "t8"), ("pr7", "t10"),
    ("pr8", "t1"), ("pr8", "t9"), ("pr8", "t11"),
    ("pr9", "t3"), ("pr9", "t12"),
    ("pr10", "t6"), ("pr10", "t9"),
    ("pr11", "t10"), ("pr11", "t15"), ("pr11", "t11"),
    ("pr12", "t4"), ("pr12", "t5"), ("pr12", "t14"),
]

# (skill_id, skill_id) - seeded in both directions
SKILL_RELATED = [
    ("sk1", "sk10"), ("sk1", "sk9"), ("sk1", "sk19"), ("sk1", "sk20"),
    ("sk3", "sk5"), ("sk3", "sk8"), ("sk3", "sk4"), ("sk3", "sk6"), ("sk3", "sk7"),
    ("sk5", "sk8"), ("sk5", "sk4"),
    ("sk11", "sk12"), ("sk11", "sk20"),
    ("sk16", "sk23"), ("sk16", "sk21"),
    ("sk21", "sk23"),
    ("sk17", "sk18"),
]

# (role_id, skill_id)
ROLE_REQUIRES = [
    ("r1", "sk3"), ("r1", "sk5"), ("r1", "sk8"), ("r1", "sk11"), ("r1", "sk14"), ("r1", "sk16"),
    ("r2", "sk1"), ("r2", "sk9"), ("r2", "sk11"), ("r2", "sk14"), ("r2", "sk15"),
    ("r3", "sk1"), ("r3", "sk8"), ("r3", "sk11"), ("r3", "sk12"), ("r3", "sk14"),
    ("r4", "sk3"), ("r4", "sk5"), ("r4", "sk6"), ("r4", "sk17"), ("r4", "sk18"),
    ("r5", "sk1"), ("r5", "sk2"), ("r5", "sk20"), ("r5", "sk15"),
    ("r6", "sk1"), ("r6", "sk11"), ("r6", "sk19"),
    ("r7", "sk1"), ("r7", "sk11"), ("r7", "sk13"), ("r7", "sk21"), ("r7", "sk16"),
    ("r8", "sk1"), ("r8", "sk19"), ("r8", "sk20"), ("r8", "sk21"),
]

# (person_id, role_id)
PERSON_INTERESTED = [
    ("p1", "r2"), ("p2", "r4"), ("p5", "r1"), ("p9", "r7"),
    ("p12", "r8"), ("p3", "r5"), ("p10", "r1"),
]


def seed_database():
    """Populate CognoDB with constraints and sample data. Uses MERGE
    throughout so this can be run repeatedly without creating
    duplicate nodes or relationships."""
    if not db.is_connected:
        logger.error("Cannot seed: no database connection.")
        return False

    logger.info("Creating uniqueness constraints...")
    constraints = [
        "CREATE CONSTRAINT person_id IF NOT EXISTS FOR (p:Person) REQUIRE p.id IS UNIQUE",
        "CREATE CONSTRAINT skill_id IF NOT EXISTS FOR (s:Skill) REQUIRE s.id IS UNIQUE",
        "CREATE CONSTRAINT project_id IF NOT EXISTS FOR (pr:Project) REQUIRE pr.id IS UNIQUE",
        "CREATE CONSTRAINT technology_id IF NOT EXISTS FOR (t:Technology) REQUIRE t.id IS UNIQUE",
        "CREATE CONSTRAINT jobrole_id IF NOT EXISTS FOR (r:JobRole) REQUIRE r.id IS UNIQUE",
    ]
    for stmt in constraints:
        try:
            db.run(stmt)
        except Neo4jError as exc:
            # Some CognoDB/Neo4j versions use slightly different constraint
            # syntax (e.g. `ASSERT ... IS UNIQUE` on older versions). Log
            # and continue rather than aborting the whole seed.
            logger.warning("Constraint step skipped (%s): %s", stmt, exc)

    logger.info("Seeding nodes...")
    db.run(
        "UNWIND $rows AS row MERGE (p:Person {id: row.id}) "
        "SET p.name = row.name, p.email = row.email, "
        "p.experience = row.experience, p.location = row.location",
        {"rows": PEOPLE_DATA},
    )
    db.run(
        "UNWIND $rows AS row MERGE (s:Skill {id: row.id}) "
        "SET s.name = row.name, s.category = row.category, s.level = row.level",
        {"rows": SKILLS_DATA},
    )
    db.run(
        "UNWIND $rows AS row MERGE (pr:Project {id: row.id}) "
        "SET pr.name = row.name, pr.description = row.description, "
        "pr.difficulty = row.difficulty",
        {"rows": PROJECTS_DATA},
    )
    db.run(
        "UNWIND $rows AS row MERGE (t:Technology {id: row.id}) "
        "SET t.name = row.name, t.category = row.category",
        {"rows": TECH_DATA},
    )
    db.run(
        "UNWIND $rows AS row MERGE (r:JobRole {id: row.id}) "
        "SET r.title = row.title, r.department = row.department, "
        "r.experience_level = row.experience_level",
        {"rows": ROLES_DATA},
    )

    logger.info("Seeding relationships...")
    db.run(
        "UNWIND $rows AS row MATCH (p:Person {id: row[0]}), (s:Skill {id: row[1]}) "
        "MERGE (p)-[:HAS_SKILL]->(s)",
        {"rows": [list(pair) for pair in PERSON_SKILLS]},
    )
    db.run(
        "UNWIND $rows AS row MATCH (p:Person {id: row[0]}), (pr:Project {id: row[1]}) "
        "MERGE (p)-[:WORKED_ON]->(pr)",
        {"rows": [list(pair) for pair in PERSON_PROJECTS]},
    )
    db.run(
        "UNWIND $rows AS row MATCH (pr:Project {id: row[0]}), (t:Technology {id: row[1]}) "
        "MERGE (pr)-[:USES]->(t)",
        {"rows": [list(pair) for pair in PROJECT_TECH]},
    )
    db.run(
        "UNWIND $rows AS row MATCH (r:JobRole {id: row[0]}), (s:Skill {id: row[1]}) "
        "MERGE (r)-[:REQUIRES]->(s)",
        {"rows": [list(pair) for pair in ROLE_REQUIRES]},
    )
    db.run(
        "UNWIND $rows AS row MATCH (p:Person {id: row[0]}), (r:JobRole {id: row[1]}) "
        "MERGE (p)-[:INTERESTED_IN]->(r)",
        {"rows": [list(pair) for pair in PERSON_INTERESTED]},
    )
    # RELATED_TO is seeded symmetrically (both directions) so traversal
    # works regardless of which of the two skills you start from.
    both_ways = [list(pair) for pair in SKILL_RELATED] + [
        [b, a] for (a, b) in SKILL_RELATED
    ]
    db.run(
        "UNWIND $rows AS row MATCH (a:Skill {id: row[0]}), (b:Skill {id: row[1]}) "
        "MERGE (a)-[:RELATED_TO]->(b)",
        {"rows": both_ways},
    )

    logger.info("Seeding complete.")
    return True


# ======================================================================
# 6. API ROUTES
# ======================================================================
@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")


@app.route("/api/stats")
@api_route
def get_stats():
    counts = {}
    for key, query in STATS_QUERIES.items():
        rows = db.run(query)
        counts[key] = rows[0]["c"] if rows else 0
    return jsonify(counts)


@app.route("/api/people")
@api_route
def get_people():
    search = request.args.get("search", "").strip()
    if search:
        rows = db.run(
            "MATCH (p:Person) WHERE toLower(p.name) CONTAINS toLower($q) "
            "OPTIONAL MATCH (p)-[:HAS_SKILL]->(s:Skill) "
            "OPTIONAL MATCH (p)-[:WORKED_ON]->(pr:Project) "
            "RETURN p, count(DISTINCT s) AS skillCount, count(DISTINCT pr) AS projectCount "
            "ORDER BY p.name",
            {"q": search},
        )
    else:
        rows = db.run(
            "MATCH (p:Person) "
            "OPTIONAL MATCH (p)-[:HAS_SKILL]->(s:Skill) "
            "OPTIONAL MATCH (p)-[:WORKED_ON]->(pr:Project) "
            "RETURN p, count(DISTINCT s) AS skillCount, count(DISTINCT pr) AS projectCount "
            "ORDER BY p.name"
        )
    people = []
    for row in rows:
        person = node_to_dict(row["p"])
        person["skillCount"] = row["skillCount"]
        person["projectCount"] = row["projectCount"]
        people.append(person)
    return jsonify(people)


@app.route("/api/people/<person_id>")
@api_route
def get_person(person_id):
    rows = db.run("MATCH (p:Person {id: $id}) RETURN p", {"id": person_id})
    if not rows:
        return error_response("Person not found.", 404)
    person = node_to_dict(rows[0]["p"])

    skills = db.run(
        "MATCH (p:Person {id: $id})-[:HAS_SKILL]->(s:Skill) RETURN s ORDER BY s.name",
        {"id": person_id},
    )
    projects = db.run(
        "MATCH (p:Person {id: $id})-[:WORKED_ON]->(pr:Project) RETURN pr ORDER BY pr.name",
        {"id": person_id},
    )
    technologies = db.run(
        "MATCH (p:Person {id: $id})-[:WORKED_ON]->(:Project)-[:USES]->(t:Technology) "
        "RETURN DISTINCT t ORDER BY t.name",
        {"id": person_id},
    )

    person["skills"] = [node_to_dict(r["s"]) for r in skills]
    person["projects"] = [node_to_dict(r["pr"]) for r in projects]
    person["technologies"] = [node_to_dict(r["t"]) for r in technologies]
    return jsonify(person)


@app.route("/api/skills")
@api_route
def get_skills():
    rows = db.run("MATCH (s:Skill) RETURN s ORDER BY s.name")
    return jsonify([node_to_dict(r["s"]) for r in rows])


@app.route("/api/skills/<skill_id>")
@api_route
def get_skill(skill_id):
    rows = db.run("MATCH (s:Skill {id: $id}) RETURN s", {"id": skill_id})
    if not rows:
        return error_response("Skill not found.", 404)
    skill = node_to_dict(rows[0]["s"])

    people = db.run(
        "MATCH (p:Person)-[:HAS_SKILL]->(s:Skill {id: $id}) RETURN p ORDER BY p.name",
        {"id": skill_id},
    )
    related = db.run(
        "MATCH (s:Skill {id: $id})-[:RELATED_TO]->(rel:Skill) RETURN rel ORDER BY rel.name",
        {"id": skill_id},
    )
    roles = db.run(
        "MATCH (r:JobRole)-[:REQUIRES]->(s:Skill {id: $id}) RETURN r ORDER BY r.title",
        {"id": skill_id},
    )
    projects = db.run(
        "MATCH (s:Skill {id: $id}) "
        "MATCH (t:Technology) WHERE toLower(t.name) = toLower(s.name) "
        "MATCH (t)<-[:USES]-(pr:Project) "
        "RETURN DISTINCT pr ORDER BY pr.name",
        {"id": skill_id},
    )

    skill["people"] = [node_to_dict(r["p"]) for r in people]
    skill["relatedSkills"] = [node_to_dict(r["rel"]) for r in related]
    skill["jobRoles"] = [node_to_dict(r["r"]) for r in roles]
    skill["projects"] = [node_to_dict(r["pr"]) for r in projects]
    return jsonify(skill)


@app.route("/api/projects")
@api_route
def get_projects():
    rows = db.run("MATCH (pr:Project) RETURN pr ORDER BY pr.name")
    return jsonify([node_to_dict(r["pr"]) for r in rows])


@app.route("/api/projects/<project_id>")
@api_route
def get_project(project_id):
    rows = db.run("MATCH (pr:Project {id: $id}) RETURN pr", {"id": project_id})
    if not rows:
        return error_response("Project not found.", 404)
    project = node_to_dict(rows[0]["pr"])

    tech = db.run(
        "MATCH (pr:Project {id: $id})-[:USES]->(t:Technology) RETURN t ORDER BY t.name",
        {"id": project_id},
    )
    people = db.run(
        "MATCH (p:Person)-[:WORKED_ON]->(pr:Project {id: $id}) RETURN p ORDER BY p.name",
        {"id": project_id},
    )

    project["technologies"] = [node_to_dict(r["t"]) for r in tech]
    project["people"] = [node_to_dict(r["p"]) for r in people]
    return jsonify(project)


@app.route("/api/career/<person_id>")
@api_route
def get_career(person_id):
    person_rows = db.run("MATCH (p:Person {id: $id}) RETURN p", {"id": person_id})
    if not person_rows:
        return error_response("Person not found.", 404)

    skill_rows = db.run(
        "MATCH (p:Person {id: $id})-[:HAS_SKILL]->(s:Skill) RETURN s ORDER BY s.name",
        {"id": person_id},
    )
    current_skills = [node_to_dict(r["s"]) for r in skill_rows]
    current_skill_ids = {s["id"] for s in current_skills}

    # F. Job role matching — GRAPH-SPECIFIC QUERY (Career Path Explorer):
    # Person -HAS_SKILL-> Skill <-REQUIRES- JobRole, comparing sets of
    # skills to determine the closest career fit. This kind of "compare
    # this person's skill-set against every role's requirement-set" query
    # is awkward in SQL (it needs several join + group-by + set
    # operations across a many-to-many relationship) but falls out
    # naturally as a graph traversal here.
    role_rows = db.run(
        "MATCH (r:JobRole)-[:REQUIRES]->(rs:Skill) RETURN r, collect(rs) AS required "
        "ORDER BY r.title",
    )
    roles = []
    for row in role_rows:
        role = node_to_dict(row["r"])
        required = [node_to_dict(s) for s in row["required"]]
        matched, missing, percentage = calc_match(current_skill_ids, required)
        role["matchedSkills"] = matched
        role["missingSkills"] = missing
        role["matchPercentage"] = percentage
        roles.append(role)
    roles.sort(key=lambda r: r["matchPercentage"], reverse=True)

    # G. Multi-hop skill recommendation:
    # Person -HAS_SKILL-> Skill -RELATED_TO-> Skill (not already known)
    rec_rows = db.run(
        "MATCH (p:Person {id: $id})-[:HAS_SKILL]->(s:Skill)-[:RELATED_TO]->(rel:Skill) "
        "WHERE NOT (p)-[:HAS_SKILL]->(rel) "
        "RETURN rel, collect(DISTINCT s.name) AS becauseOf",
        {"id": person_id},
    )
    recommendations = []
    seen = set()
    for row in rec_rows:
        rel = node_to_dict(row["rel"])
        if rel["id"] in seen:
            continue
        seen.add(rel["id"])
        rel["becauseOf"] = row["becauseOf"]
        recommendations.append(rel)

    return jsonify(
        {
            "person": node_to_dict(person_rows[0]["p"]),
            "currentSkills": current_skills,
            "recommendedRoles": roles,
            "recommendedSkills": recommendations,
        }
    )


@app.route("/api/graph/overview")
@api_route
def get_graph_overview():
    """A readable sample subgraph for the dashboard's 'Explore the
    Graph' panel — the whole dataset is small enough to show safely."""
    rows = db.run(
        "MATCH (n) OPTIONAL MATCH (n)-[r]->(m) "
        "RETURN n, labels(n) AS nLabels, r, type(r) AS rType, m, labels(m) AS mLabels "
        "LIMIT 400"
    )
    return jsonify(_rows_to_graph(rows))


@app.route("/api/graph/<node_type>/<node_id>")
@api_route
def get_graph_for_node(node_type, node_id):
    """H. Graph exploration query: given a node, return everything
    connected to it (1 hop) so the front end can render it and let the
    user click deeper from there."""
    query = GRAPH_NODE_QUERIES.get(node_type)
    if query is None:
        return error_response("Unknown node type.", 400)

    rows = db.run(query, {"id": node_id})
    if not rows:
        return error_response("Node not found.", 404)
    return jsonify(_rows_to_graph(rows))


def _rows_to_graph(rows):
    """Shared helper: turn rows of (n, r, m) into cytoscape-friendly
    node/edge lists, deduplicated."""
    nodes = {}
    edges = {}
    for row in rows:
        n, m, r = row.get("n"), row.get("m"), row.get("r")
        if n is not None:
            n_data = node_to_dict(n)
            n_type = (row.get("nLabels") or ["Node"])[0]
            nodes[n_data["id"]] = {"id": n_data["id"], "label": _display_name(n_data), "type": n_type}
        if m is not None:
            m_data = node_to_dict(m)
            m_type = (row.get("mLabels") or ["Node"])[0]
            nodes[m_data["id"]] = {"id": m_data["id"], "label": _display_name(m_data), "type": m_type}
        if r is not None and n is not None and m is not None:
            n_data, m_data = node_to_dict(n), node_to_dict(m)
            edge_id = f"{n_data['id']}-{row.get('rType')}-{m_data['id']}"
            edges[edge_id] = {
                "id": edge_id,
                "source": n_data["id"],
                "target": m_data["id"],
                "label": row.get("rType"),
            }
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _display_name(node_data):
    return node_data.get("name") or node_data.get("title") or node_data.get("id")


@app.route("/api/search")
@api_route
def search_all():
    """A single search box across People, Skills, Projects, Technologies
    and Job Roles."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"people": [], "skills": [], "projects": [], "technologies": [], "jobRoles": []})

    def find(key):
        rows = db.run(SEARCH_QUERIES[key], {"q": q})
        return [node_to_dict(r["n"]) for r in rows]

    return jsonify(
        {
            "people": find("people"),
            "skills": find("skills"),
            "projects": find("projects"),
            "technologies": find("technologies"),
            "jobRoles": find("jobRoles"),
        }
    )


@app.route("/api/people/<person_id>/shared-skills")
@api_route
def get_shared_skills(person_id):
    """I. Find people who share skills with the given person."""
    rows = db.run(
        "MATCH (p1:Person {id: $id})-[:HAS_SKILL]->(s:Skill)<-[:HAS_SKILL]-(p2:Person) "
        "WHERE p1 <> p2 "
        "RETURN p2, collect(DISTINCT s.name) AS sharedSkills "
        "ORDER BY size(collect(DISTINCT s.name)) DESC",
        {"id": person_id},
    )
    results = []
    for row in rows:
        person = node_to_dict(row["p2"])
        person["sharedSkills"] = row["sharedSkills"]
        results.append(person)
    return jsonify(results)


@app.route("/api/technologies/<tech_id>/projects")
@api_route
def get_projects_for_tech(tech_id):
    """J. Find projects related to a selected technology."""
    rows = db.run(
        "MATCH (t:Technology {id: $id})<-[:USES]-(pr:Project) RETURN pr ORDER BY pr.name",
        {"id": tech_id},
    )
    return jsonify([node_to_dict(r["pr"]) for r in rows])


@app.route("/api/health")
def health():
    return jsonify({"database": "connected" if db.is_connected else "disconnected"})


# ======================================================================
# 7. HTML / CSS / JS FRONT END
# ======================================================================
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SkillGraph — Skill &amp; Career Relationship Explorer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<style>
:root{
  --bg:#0b1220; --bg-panel:#111a2e; --bg-card:#141f38; --bg-hover:#1b2846;
  --border:#22304f; --text:#e7ecf7; --text-dim:#93a1c2; --text-faint:#5c6a8c;
  --accent:#6c8dfb; --accent-2:#38d9c9; --accent-3:#f7b955; --danger:#f0637a;
  --radius:12px; --shadow:0 8px 24px rgba(0,0,0,.35);
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{
  background:var(--bg); color:var(--text);
  font-family:'Inter',system-ui,-apple-system,sans-serif;
  font-size:14px; line-height:1.5;
}
a{color:inherit;}
.app{display:flex; min-height:100vh;}

/* ---------- Sidebar ---------- */
.sidebar{
  width:230px; flex-shrink:0; background:var(--bg-panel);
  border-right:1px solid var(--border); padding:22px 14px;
  display:flex; flex-direction:column; gap:4px;
  position:sticky; top:0; height:100vh;
}
.brand{display:flex; align-items:center; gap:10px; padding:4px 10px 22px;}
.brand-mark{
  width:34px;height:34px;border-radius:9px;
  background:linear-gradient(135deg,var(--accent),var(--accent-2));
  display:flex;align-items:center;justify-content:center;font-weight:800;color:#0b1220;
}
.brand-name{font-weight:800; font-size:17px; letter-spacing:.2px;}
.nav-item{
  display:flex; align-items:center; gap:10px; padding:10px 12px;
  border-radius:9px; cursor:pointer; color:var(--text-dim); font-weight:500;
  transition:background .15s ease, color .15s ease;
}
.nav-item:hover{background:var(--bg-hover); color:var(--text);}
.nav-item.active{background:var(--bg-hover); color:var(--text); box-shadow:inset 3px 0 0 var(--accent);}
.nav-dot{width:7px;height:7px;border-radius:50%;background:currentColor;opacity:.6;}
.sidebar-footer{margin-top:auto; padding:12px 10px; color:var(--text-faint); font-size:12px;}

/* ---------- Main ---------- */
.main{flex:1; min-width:0; padding:26px 34px 60px;}
.topbar{display:flex; align-items:center; justify-content:space-between; gap:20px; margin-bottom:22px; flex-wrap:wrap;}
.page-title{font-size:22px; font-weight:800; margin:0;}
.page-subtitle{color:var(--text-dim); margin:4px 0 0; font-size:13.5px;}
.search-box{position:relative; width:320px; max-width:100%;}
.search-box input{
  width:100%; padding:10px 14px 10px 38px; border-radius:10px; border:1px solid var(--border);
  background:var(--bg-card); color:var(--text); font-size:13.5px; outline:none;
}
.search-box input:focus{border-color:var(--accent);}
.search-box::before{content:"⌕"; position:absolute; left:13px; top:50%; transform:translateY(-50%); color:var(--text-faint); font-size:16px;}
.search-results{
  position:absolute; top:calc(100% + 6px); left:0; right:0; background:var(--bg-card);
  border:1px solid var(--border); border-radius:10px; box-shadow:var(--shadow); z-index:40;
  max-height:360px; overflow:auto; display:none;
}
.search-results.show{display:block;}
.search-group-label{padding:8px 12px 2px; font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:var(--text-faint);}
.search-result-item{padding:9px 12px; cursor:pointer; display:flex; justify-content:space-between; gap:8px;}
.search-result-item:hover{background:var(--bg-hover);}

.section{display:none;}
.section.active{display:block;}

/* ---------- Cards ---------- */
.cards-grid{display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin-bottom:26px;}
.stat-card{
  background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius);
  padding:18px; display:flex; flex-direction:column; gap:6px;
}
.stat-card .value{font-size:26px; font-weight:800;}
.stat-card .label{color:var(--text-dim); font-size:12.5px;}
.stat-card .icon{font-size:18px; opacity:.85;}

.panel{
  background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius);
  padding:20px; margin-bottom:22px;
}
.panel-header{display:flex; align-items:center; justify-content:space-between; margin-bottom:14px;}
.panel-title{font-weight:700; font-size:15.5px;}
.panel-actions{display:flex; gap:8px;}
.btn{
  padding:7px 13px; border-radius:8px; border:1px solid var(--border); background:var(--bg-hover);
  color:var(--text); cursor:pointer; font-size:12.5px; font-weight:600; transition:all .15s ease;
}
.btn:hover{border-color:var(--accent); color:var(--accent);}
.btn.primary{background:var(--accent); border-color:var(--accent); color:#0b1220;}
.btn.primary:hover{filter:brightness(1.08); color:#0b1220;}

/* ---------- Grid list (people/skills/projects) ---------- */
.grid-list{display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:14px;}
.entity-card{
  background:var(--bg-panel); border:1px solid var(--border); border-radius:10px; padding:15px;
  cursor:pointer; transition:border-color .15s ease, transform .15s ease;
}
.entity-card:hover{border-color:var(--accent); transform:translateY(-2px);}
.entity-card .name{font-weight:700; font-size:14.5px; margin-bottom:4px;}
.entity-card .meta{color:var(--text-dim); font-size:12px; margin-bottom:8px;}
.chip-row{display:flex; flex-wrap:wrap; gap:6px;}
.chip{
  background:rgba(108,141,251,.12); color:var(--accent); border:1px solid rgba(108,141,251,.3);
  border-radius:20px; padding:3px 9px; font-size:11px; font-weight:600;
}
.chip.tone-2{background:rgba(56,217,201,.12); color:var(--accent-2); border-color:rgba(56,217,201,.3);}
.chip.tone-3{background:rgba(247,185,85,.12); color:var(--accent-3); border-color:rgba(247,185,85,.3);}

/* ---------- Filter bar ---------- */
.filter-bar{display:flex; gap:10px; margin-bottom:16px; flex-wrap:wrap;}
.filter-bar input, .filter-bar select{
  padding:9px 12px; border-radius:8px; border:1px solid var(--border); background:var(--bg-card);
  color:var(--text); font-size:13px; outline:none;
}
.filter-bar input:focus, .filter-bar select:focus{border-color:var(--accent);}

/* ---------- Detail panel / modal ---------- */
.overlay{
  position:fixed; inset:0; background:rgba(6,10,20,.6); display:none; align-items:flex-start;
  justify-content:center; padding:40px 20px; z-index:100; overflow:auto;
}
.overlay.show{display:flex;}
.detail-panel{
  background:var(--bg-panel); border:1px solid var(--border); border-radius:14px; width:min(760px,100%);
  padding:26px; box-shadow:var(--shadow);
}
.detail-panel-header{display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:16px;}
.detail-title{font-size:19px; font-weight:800; margin:0 0 4px;}
.detail-sub{color:var(--text-dim); font-size:13px;}
.close-btn{background:none; border:none; color:var(--text-dim); font-size:20px; cursor:pointer; line-height:1;}
.close-btn:hover{color:var(--text);}
.detail-section{margin-top:18px;}
.detail-section h4{margin:0 0 8px; font-size:12.5px; text-transform:uppercase; letter-spacing:.5px; color:var(--text-faint);}

/* ---------- Progress bar ---------- */
.role-row{background:var(--bg-card); border:1px solid var(--border); border-radius:10px; padding:13px 15px; margin-bottom:10px;}
.role-row-top{display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;}
.role-title{font-weight:700; font-size:13.5px;}
.role-pct{font-weight:800; font-size:13px; color:var(--accent-2);}
.progress-track{height:7px; border-radius:5px; background:var(--border); overflow:hidden; margin-bottom:9px;}
.progress-fill{height:100%; background:linear-gradient(90deg,var(--accent),var(--accent-2)); border-radius:5px;}
.role-skills{display:flex; flex-wrap:wrap; gap:6px;}

/* ---------- Graph ---------- */
#cy{width:100%; height:460px; background:var(--bg-panel); border-radius:10px; border:1px solid var(--border);}
.graph-toolbar{display:flex; gap:8px; margin-bottom:10px;}
.legend{display:flex; gap:14px; margin-top:10px; flex-wrap:wrap; font-size:12px; color:var(--text-dim);}
.legend-item{display:flex; align-items:center; gap:6px;}
.legend-dot{width:10px; height:10px; border-radius:50%;}

/* ---------- Toast ---------- */
.toast-wrap{position:fixed; bottom:22px; right:22px; display:flex; flex-direction:column; gap:10px; z-index:200;}
.toast{
  background:var(--bg-card); border:1px solid var(--border); border-left:4px solid var(--danger);
  padding:12px 16px; border-radius:9px; box-shadow:var(--shadow); font-size:13px; max-width:320px;
  animation:slidein .2s ease;
}
@keyframes slidein{from{transform:translateX(20px); opacity:0;} to{transform:translateX(0); opacity:1;}}

/* ---------- Loading / empty states ---------- */
.loading, .empty-state{
  display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px;
  padding:40px 10px; color:var(--text-dim); text-align:center;
}
.spinner{
  width:26px; height:26px; border-radius:50%; border:3px solid var(--border);
  border-top-color:var(--accent); animation:spin .7s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg);}}
.empty-state .icon{font-size:26px; opacity:.6;}

/* ---------- Career explorer ---------- */
.person-select-row{display:flex; gap:10px; align-items:center; margin-bottom:18px; flex-wrap:wrap;}
.person-select-row select{flex:1; min-width:220px;}
.two-col{display:grid; grid-template-columns:1.1fr 1fr; gap:20px;}
@media(max-width:900px){.two-col{grid-template-columns:1fr;}}

::-webkit-scrollbar{width:9px; height:9px;}
::-webkit-scrollbar-thumb{background:var(--border); border-radius:6px;}

@media(max-width:1150px){.cards-grid{grid-template-columns:repeat(3,1fr);}}
@media(max-width:760px){
  .app{flex-direction:column;}
  .sidebar{width:100%; height:auto; position:relative; flex-direction:row; overflow-x:auto; padding:12px;}
  .brand{padding:0 12px 0 0;}
  .sidebar-footer{display:none;}
  .cards-grid{grid-template-columns:repeat(2,1fr);}
  .main{padding:18px;}
}
</style>
</head>
<body>
<div class="app">

  <!-- ============ SIDEBAR ============ -->
  <div class="sidebar">
    <div class="brand">
      <div class="brand-mark">SG</div>
      <div class="brand-name">SkillGraph</div>
    </div>
    <div class="nav-item active" data-section="dashboard"><span class="nav-dot"></span>Dashboard</div>
    <div class="nav-item" data-section="people"><span class="nav-dot"></span>People</div>
    <div class="nav-item" data-section="skills"><span class="nav-dot"></span>Skills</div>
    <div class="nav-item" data-section="projects"><span class="nav-dot"></span>Projects</div>
    <div class="nav-item" data-section="career"><span class="nav-dot"></span>Career Explorer</div>
    <div class="nav-item" data-section="graph"><span class="nav-dot"></span>Graph Explorer</div>
    <div class="sidebar-footer">Powered by CognoDB<br>&amp; the Neo4j driver</div>
  </div>

  <!-- ============ MAIN ============ -->
  <div class="main">

    <div class="topbar">
      <div>
        <h1 class="page-title" id="pageTitle">Skill &amp; Career Relationship Explorer</h1>
        <p class="page-subtitle">Explore the connections between skills, projects and careers.</p>
      </div>
      <div class="search-box">
        <input type="text" id="globalSearch" placeholder="Search people, skills, projects...">
        <div class="search-results" id="searchResults"></div>
      </div>
    </div>

    <!-- ===== DASHBOARD ===== -->
    <div class="section active" id="section-dashboard">
      <div class="cards-grid" id="statsCards"></div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">Explore the Graph</div>
          <div class="panel-actions">
            <button class="btn" onclick="graphZoom(1.2)">Zoom In</button>
            <button class="btn" onclick="graphZoom(0.8)">Zoom Out</button>
            <button class="btn" onclick="graphFit()">Fit</button>
            <button class="btn" onclick="loadOverviewGraph()">Reset</button>
          </div>
        </div>
        <div id="dashGraphWrap"><div id="cy-dashboard" style="width:100%;height:420px;background:var(--bg-panel);border-radius:10px;border:1px solid var(--border);"></div></div>
        <div class="legend" id="dashLegend"></div>
      </div>

      <div class="panel">
        <div class="panel-header"><div class="panel-title">Career Insights</div></div>
        <div id="dashCareerInsights"><div class="loading"><div class="spinner"></div>Loading insights...</div></div>
      </div>
    </div>

    <!-- ===== PEOPLE ===== -->
    <div class="section" id="section-people">
      <div class="filter-bar">
        <input type="text" id="peopleFilter" placeholder="Filter people by name..." oninput="loadPeople()">
      </div>
      <div id="peopleList"><div class="loading"><div class="spinner"></div>Loading people...</div></div>
    </div>

    <!-- ===== SKILLS ===== -->
    <div class="section" id="section-skills">
      <div class="filter-bar">
        <input type="text" id="skillsFilter" placeholder="Filter skills by name..." oninput="renderSkills()">
        <select id="skillsCategoryFilter" onchange="renderSkills()"><option value="">All categories</option></select>
      </div>
      <div id="skillsList"><div class="loading"><div class="spinner"></div>Loading skills...</div></div>
    </div>

    <!-- ===== PROJECTS ===== -->
    <div class="section" id="section-projects">
      <div id="projectsList"><div class="loading"><div class="spinner"></div>Loading projects...</div></div>
    </div>

    <!-- ===== CAREER EXPLORER ===== -->
    <div class="section" id="section-career">
      <div class="panel">
        <div class="person-select-row">
          <select id="careerPersonSelect" onchange="loadCareer(this.value)"><option value="">Select a person...</option></select>
        </div>
        <div id="careerContent">
          <div class="empty-state"><div class="icon">🎯</div>Select a person to see career recommendations.</div>
        </div>
      </div>
    </div>

    <!-- ===== GRAPH EXPLORER ===== -->
    <div class="section" id="section-graph">
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">Graph Explorer</div>
          <div class="panel-actions">
            <button class="btn" onclick="graphExplorerZoom(1.2)">Zoom In</button>
            <button class="btn" onclick="graphExplorerZoom(0.8)">Zoom Out</button>
            <button class="btn" onclick="graphExplorerFit()">Fit</button>
            <button class="btn" onclick="loadExplorerOverview()">Reset</button>
          </div>
        </div>
        <div class="filter-bar">
          <select id="explorerTypeSelect"><option value="Person">Person</option><option value="Skill">Skill</option><option value="Project">Project</option><option value="Technology">Technology</option><option value="JobRole">Job Role</option></select>
          <select id="explorerNodeSelect"></select>
          <button class="btn primary" onclick="exploreSelectedNode()">Explore</button>
        </div>
        <div id="cy-explorer" style="width:100%;height:460px;background:var(--bg-panel);border-radius:10px;border:1px solid var(--border);"></div>
        <div class="legend" id="explorerLegend"></div>
      </div>
      <div class="panel" id="explorerInfoPanel" style="display:none;"></div>
    </div>

  </div>
</div>

<!-- ===== DETAIL OVERLAY ===== -->
<div class="overlay" id="detailOverlay" onclick="if(event.target===this) closeDetail();">
  <div class="detail-panel" id="detailPanel"></div>
</div>

<div class="toast-wrap" id="toastWrap"></div>

<script>
/* ======================================================================
   Front-end application logic (vanilla JS).
   All data comes from the Flask JSON API defined in app.py.
====================================================================== */
const NODE_COLORS = {
  Person: "#6c8dfb", Skill: "#38d9c9", Project: "#f7b955",
  Technology: "#c893fb", JobRole: "#f0637a"
};

let allPeopleCache = [];
let allSkillsCache = [];
let cyDashboard = null;
let cyExplorer = null;

// ---------------- Navigation ----------------
document.querySelectorAll(".nav-item").forEach(item => {
  item.addEventListener("click", () => switchSection(item.dataset.section));
});

function switchSection(name) {
  document.querySelectorAll(".nav-item").forEach(i => i.classList.toggle("active", i.dataset.section === name));
  document.querySelectorAll(".section").forEach(s => s.classList.remove("active"));
  document.getElementById("section-" + name).classList.add("active");
  const titles = {
    dashboard: "Skill & Career Relationship Explorer", people: "People Explorer",
    skills: "Skill Explorer", projects: "Project Explorer",
    career: "Career Explorer", graph: "Graph Explorer"
  };
  document.getElementById("pageTitle").textContent = titles[name];
  if (name === "people" && allPeopleCache.length === 0) loadPeople();
  if (name === "skills" && allSkillsCache.length === 0) loadSkills();
  if (name === "projects") loadProjects();
  if (name === "career") loadCareerPersonOptions();
  if (name === "graph") { loadExplorerNodeOptions(); if (!cyExplorer) loadExplorerOverview(); }
}

// ---------------- Toasts ----------------
function showToast(message) {
  const wrap = document.getElementById("toastWrap");
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

async function apiGet(url) {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => ({error: "Something went wrong."}));
    throw new Error(body.error || "Request failed.");
  }
  return res.json();
}

// ---------------- Dashboard ----------------
async function loadStats() {
  const el = document.getElementById("statsCards");
  try {
    const stats = await apiGet("/api/stats");
    const cards = [
      {label: "People", value: stats.people, icon: "👤"},
      {label: "Skills", value: stats.skills, icon: "🧠"},
      {label: "Projects", value: stats.projects, icon: "📁"},
      {label: "Technologies", value: stats.technologies, icon: "⚙️"},
      {label: "Job Roles", value: stats.jobRoles, icon: "💼"},
    ];
    el.innerHTML = cards.map(c => `
      <div class="stat-card">
        <div class="icon">${c.icon}</div>
        <div class="value">${c.value}</div>
        <div class="label">${c.label}</div>
      </div>`).join("");
  } catch (e) {
    el.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="icon">⚠️</div>${e.message}</div>`;
    showToast(e.message);
  }
}

async function loadOverviewGraph() {
  const wrap = document.getElementById("dashGraphWrap");
  wrap.innerHTML = '<div class="loading"><div class="spinner"></div>Loading graph...</div><div id="cy-dashboard" style="width:100%;height:420px;"></div>';
  try {
    const data = await apiGet("/api/graph/overview");
    renderGraph("cy-dashboard", data, (id) => "cyDashboard");
    renderLegend("dashLegend");
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
  }
}

async function loadDashboardCareerInsights() {
  const el = document.getElementById("dashCareerInsights");
  try {
    const people = await apiGet("/api/people");
    if (!people.length) { el.innerHTML = '<div class="empty-state">No people found.</div>'; return; }
    const sample = people[0];
    const career = await apiGet(`/api/career/${sample.id}`);
    const top = career.recommendedRoles.slice(0, 3);
    el.innerHTML = `<p class="detail-sub" style="margin-bottom:12px;">Top matches for <strong style="color:var(--text)">${sample.name}</strong> based on current skills:</p>` +
      top.map(r => roleRowHtml(r)).join("");
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
  }
}

// ---------------- People ----------------
async function loadPeople() {
  const el = document.getElementById("peopleList");
  const q = document.getElementById("peopleFilter").value;
  el.innerHTML = '<div class="loading"><div class="spinner"></div>Loading people...</div>';
  try {
    const people = await apiGet("/api/people?search=" + encodeURIComponent(q));
    allPeopleCache = people;
    if (!people.length) { el.innerHTML = '<div class="empty-state"><div class="icon">🔍</div>No people found.</div>'; return; }
    el.innerHTML = `<div class="grid-list">${people.map(p => `
      <div class="entity-card" onclick="openPersonDetail('${p.id}')">
        <div class="name">${p.name}</div>
        <div class="meta">${p.location} · ${p.experience} yrs experience</div>
        <div class="chip-row">
          <span class="chip">${p.skillCount} skills</span>
          <span class="chip tone-2">${p.projectCount} projects</span>
        </div>
      </div>`).join("")}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
    showToast(e.message);
  }
}

async function openPersonDetail(id) {
  openDetailShell("Loading person...");
  try {
    const p = await apiGet(`/api/people/${id}`);
    const career = await apiGet(`/api/career/${id}`).catch(() => null);
    const topRoles = career ? career.recommendedRoles.slice(0, 3) : [];
    const missing = topRoles[0] ? topRoles[0].missingSkills : [];
    document.getElementById("detailPanel").innerHTML = `
      <div class="detail-panel-header">
        <div><h3 class="detail-title">${p.name}</h3><div class="detail-sub">${p.location} · ${p.experience} yrs experience · ${p.email}</div></div>
        <button class="close-btn" onclick="closeDetail()">✕</button>
      </div>
      <div class="detail-section"><h4>Skills</h4>
        <div class="chip-row">${p.skills.length ? p.skills.map(s => `<span class="chip">${s.name}</span>`).join("") : '<span class="detail-sub">No skills recorded.</span>'}</div>
      </div>
      <div class="detail-section"><h4>Projects</h4>
        <div class="chip-row">${p.projects.length ? p.projects.map(pr => `<span class="chip tone-2">${pr.name}</span>`).join("") : '<span class="detail-sub">No projects recorded.</span>'}</div>
      </div>
      <div class="detail-section"><h4>Technologies (via projects)</h4>
        <div class="chip-row">${p.technologies.length ? p.technologies.map(t => `<span class="chip tone-3">${t.name}</span>`).join("") : '<span class="detail-sub">No technologies found.</span>'}</div>
      </div>
      <div class="detail-section"><h4>Recommended Job Roles</h4>
        ${topRoles.length ? topRoles.map(r => roleRowHtml(r)).join("") : '<div class="empty-state">No matching career roles yet.</div>'}
      </div>
      <div class="detail-section"><h4>Missing Skills (top role)</h4>
        <div class="chip-row">${missing.length ? missing.map(s => `<span class="chip" style="color:var(--danger);border-color:var(--danger);background:rgba(240,99,122,.1)">${s.name}</span>`).join("") : '<span class="detail-sub">No gaps — fully qualified!</span>'}</div>
      </div>
      <div class="detail-section">
        <button class="btn primary" onclick="closeDetail(); switchSection('graph'); document.getElementById('explorerTypeSelect').value='Person'; loadExplorerNodeOptions(); document.getElementById('explorerNodeSelect').value='${p.id}'; exploreSelectedNode();">View in Graph Explorer</button>
      </div>
    `;
  } catch (e) {
    document.getElementById("detailPanel").innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
  }
}

function roleRowHtml(r) {
  return `<div class="role-row">
    <div class="role-row-top"><span class="role-title">${r.title}</span><span class="role-pct">${r.matchPercentage}%</span></div>
    <div class="progress-track"><div class="progress-fill" style="width:${r.matchPercentage}%"></div></div>
    <div class="role-skills">
      ${r.matchedSkills.map(s => `<span class="chip">${s.name}</span>`).join("")}
      ${r.missingSkills.map(s => `<span class="chip" style="opacity:.55">${s.name}</span>`).join("")}
    </div>
  </div>`;
}

// ---------------- Skills ----------------
async function loadSkills() {
  const el = document.getElementById("skillsList");
  el.innerHTML = '<div class="loading"><div class="spinner"></div>Loading skills...</div>';
  try {
    allSkillsCache = await apiGet("/api/skills");
    const catSelect = document.getElementById("skillsCategoryFilter");
    const cats = [...new Set(allSkillsCache.map(s => s.category))].sort();
    catSelect.innerHTML = '<option value="">All categories</option>' + cats.map(c => `<option value="${c}">${c}</option>`).join("");
    renderSkills();
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
    showToast(e.message);
  }
}

function renderSkills() {
  const el = document.getElementById("skillsList");
  const q = document.getElementById("skillsFilter").value.toLowerCase();
  const cat = document.getElementById("skillsCategoryFilter").value;
  const filtered = allSkillsCache.filter(s => s.name.toLowerCase().includes(q) && (!cat || s.category === cat));
  if (!filtered.length) { el.innerHTML = '<div class="empty-state"><div class="icon">🔍</div>No skills found.</div>'; return; }
  el.innerHTML = `<div class="grid-list">${filtered.map(s => `
    <div class="entity-card" onclick="openSkillDetail('${s.id}')">
      <div class="name">${s.name}</div>
      <div class="meta">${s.category} · ${s.level}</div>
    </div>`).join("")}</div>`;
}

async function openSkillDetail(id) {
  openDetailShell("Loading skill...");
  try {
    const s = await apiGet(`/api/skills/${id}`);
    document.getElementById("detailPanel").innerHTML = `
      <div class="detail-panel-header">
        <div><h3 class="detail-title">${s.name}</h3><div class="detail-sub">${s.category} · ${s.level}</div></div>
        <button class="close-btn" onclick="closeDetail()">✕</button>
      </div>
      <div class="detail-section"><h4>People with this skill</h4>
        <div class="chip-row">${s.people.length ? s.people.map(p => `<span class="chip">${p.name}</span>`).join("") : '<span class="detail-sub">No one has this skill yet.</span>'}</div>
      </div>
      <div class="detail-section"><h4>Projects using it</h4>
        <div class="chip-row">${s.projects.length ? s.projects.map(p => `<span class="chip tone-2">${p.name}</span>`).join("") : '<span class="detail-sub">No projects are connected to this skill.</span>'}</div>
      </div>
      <div class="detail-section"><h4>Related skills</h4>
        <div class="chip-row">${s.relatedSkills.length ? s.relatedSkills.map(r => `<span class="chip tone-3">${r.name}</span>`).join("") : '<span class="detail-sub">No related skills found.</span>'}</div>
      </div>
      <div class="detail-section"><h4>Job roles requiring it</h4>
        <div class="chip-row">${s.jobRoles.length ? s.jobRoles.map(r => `<span class="chip">${r.title}</span>`).join("") : '<span class="detail-sub">No job roles require this skill.</span>'}</div>
      </div>
    `;
  } catch (e) {
    document.getElementById("detailPanel").innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
  }
}

// ---------------- Projects ----------------
async function loadProjects() {
  const el = document.getElementById("projectsList");
  el.innerHTML = '<div class="loading"><div class="spinner"></div>Loading projects...</div>';
  try {
    const projects = await apiGet("/api/projects");
    if (!projects.length) { el.innerHTML = '<div class="empty-state">No projects found.</div>'; return; }
    el.innerHTML = `<div class="grid-list">${projects.map(p => `
      <div class="entity-card" onclick="openProjectDetail('${p.id}')">
        <div class="name">${p.name}</div>
        <div class="meta">${p.difficulty}</div>
        <div class="chip-row"><span class="chip">${p.description}</span></div>
      </div>`).join("")}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
    showToast(e.message);
  }
}

async function openProjectDetail(id) {
  openDetailShell("Loading project...");
  try {
    const p = await apiGet(`/api/projects/${id}`);
    document.getElementById("detailPanel").innerHTML = `
      <div class="detail-panel-header">
        <div><h3 class="detail-title">${p.name}</h3><div class="detail-sub">${p.difficulty}</div></div>
        <button class="close-btn" onclick="closeDetail()">✕</button>
      </div>
      <p class="detail-sub">${p.description}</p>
      <div class="detail-section"><h4>Technologies used</h4>
        <div class="chip-row">${p.technologies.length ? p.technologies.map(t => `<span class="chip tone-3">${t.name}</span>`).join("") : '<span class="detail-sub">No technologies recorded.</span>'}</div>
      </div>
      <div class="detail-section"><h4>People involved</h4>
        <div class="chip-row">${p.people.length ? p.people.map(pe => `<span class="chip">${pe.name}</span>`).join("") : '<span class="detail-sub">No one is linked to this project yet.</span>'}</div>
      </div>
    `;
  } catch (e) {
    document.getElementById("detailPanel").innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
  }
}

// ---------------- Career Explorer ----------------
async function loadCareerPersonOptions() {
  const select = document.getElementById("careerPersonSelect");
  try {
    const people = allPeopleCache.length ? allPeopleCache : await apiGet("/api/people");
    allPeopleCache = people;
    select.innerHTML = '<option value="">Select a person...</option>' + people.map(p => `<option value="${p.id}">${p.name}</option>`).join("");
  } catch (e) { showToast(e.message); }
}

async function loadCareer(personId) {
  const el = document.getElementById("careerContent");
  if (!personId) { el.innerHTML = '<div class="empty-state"><div class="icon">🎯</div>Select a person to see career recommendations.</div>'; return; }
  el.innerHTML = '<div class="loading"><div class="spinner"></div>Loading career insights...</div>';
  try {
    const data = await apiGet(`/api/career/${personId}`);
    el.innerHTML = `
      <div class="two-col">
        <div>
          <h4 style="font-size:12.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-faint);margin-bottom:10px;">Current Skills</h4>
          <div class="chip-row" style="margin-bottom:20px;">${data.currentSkills.map(s => `<span class="chip">${s.name}</span>`).join("") || '<span class="detail-sub">No skills recorded.</span>'}</div>
          <h4 style="font-size:12.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-faint);margin-bottom:10px;">Recommended Next Skills</h4>
          <div>${data.recommendedSkills.length ? data.recommendedSkills.map(s => `
            <div class="role-row"><div class="role-row-top"><span class="role-title">${s.name}</span><span class="detail-sub">${s.category}</span></div>
            <div class="detail-sub">Recommended because you already know: ${s.becauseOf.join(", ")}</div></div>
          `).join("") : '<div class="empty-state">No related skills found.</div>'}</div>
        </div>
        <div>
          <h4 style="font-size:12.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-faint);margin-bottom:10px;">Recommended Roles</h4>
          ${data.recommendedRoles.length ? data.recommendedRoles.map(r => roleRowHtml(r)).join("") : '<div class="empty-state">No matching career roles yet.</div>'}
        </div>
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
    showToast(e.message);
  }
}

// ---------------- Graph rendering (shared) ----------------
function renderGraph(containerId, data, cyRefSetter) {
  if (!data.nodes.length) {
    document.getElementById(containerId).innerHTML = '<div class="empty-state"><div class="icon">🕸️</div>No graph data to display.</div>';
    return null;
  }
  const cy = cytoscape({
    container: document.getElementById(containerId),
    elements: [
      ...data.nodes.map(n => ({data: {id: n.id, label: n.label, type: n.type}})),
      ...data.edges.map(e => ({data: {id: e.id, source: e.source, target: e.target, label: e.label}})),
    ],
    style: [
      {selector: "node", style: {
        "background-color": ele => NODE_COLORS[ele.data("type")] || "#888",
        "label": "data(label)", "color": "#e7ecf7", "font-size": "10px",
        "text-valign": "bottom", "text-margin-y": 6, "width": 26, "height": 26,
        "border-width": 2, "border-color": "#0b1220",
      }},
      {selector: "edge", style: {
        "width": 1.4, "line-color": "#33415e", "target-arrow-color": "#33415e",
        "target-arrow-shape": "triangle", "curve-style": "bezier", "opacity": 0.7,
      }},
      {selector: ".faded", style: {"opacity": 0.15}},
      {selector: ".highlighted", style: {"border-color": "#f7b955", "border-width": 3}},
    ],
    layout: {name: "cose", animate: false, padding: 30},
  });
  cy.on("tap", "node", evt => {
    const node = evt.target;
    cy.elements().addClass("faded");
    node.removeClass("faded").addClass("highlighted");
    node.neighborhood().removeClass("faded");
    showNodeInfo(node.data());
  });
  cy.on("tap", evt => { if (evt.target === cy) { cy.elements().removeClass("faded highlighted"); } });
  return cy;
}

function showNodeInfo(nodeData) {
  const panel = document.getElementById("explorerInfoPanel");
  if (!panel) return;
  panel.style.display = "block";
  panel.innerHTML = `
    <div class="panel-header"><div class="panel-title">${nodeData.label}</div>
      <span class="chip" style="background:${NODE_COLORS[nodeData.type]}22;color:${NODE_COLORS[nodeData.type]};border-color:${NODE_COLORS[nodeData.type]}55">${nodeData.type}</span>
    </div>
    <button class="btn primary" onclick="exploreNode('${nodeData.type}','${nodeData.id}')">Explore this node</button>
  `;
}

function renderLegend(elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = Object.entries(NODE_COLORS).map(([type, color]) => `
    <div class="legend-item"><span class="legend-dot" style="background:${color}"></span>${type}</div>
  `).join("");
}

function graphZoom(factor) { if (cyDashboardInstance) cyDashboardInstance.zoom(cyDashboardInstance.zoom() * factor); }
function graphFit() { if (cyDashboardInstance) cyDashboardInstance.fit(undefined, 30); }
let cyDashboardInstance = null;

function graphExplorerZoom(factor) { if (cyExplorer) cyExplorer.zoom(cyExplorer.zoom() * factor); }
function graphExplorerFit() { if (cyExplorer) cyExplorer.fit(undefined, 30); }

// ---------------- Graph Explorer section ----------------
async function loadExplorerNodeOptions() {
  const typeSelect = document.getElementById("explorerTypeSelect");
  const nodeSelect = document.getElementById("explorerNodeSelect");
  const type = typeSelect.value;
  const endpointMap = {Person: "/api/people", Skill: "/api/skills", Project: "/api/projects", Technology: "/api/skills", JobRole: "/api/skills"};
  try {
    let items = [];
    if (type === "Person") items = allPeopleCache.length ? allPeopleCache : await apiGet("/api/people");
    else if (type === "Skill") items = allSkillsCache.length ? allSkillsCache : await apiGet("/api/skills");
    else if (type === "Project") items = await apiGet("/api/projects");
    else if (type === "Technology") items = (await apiGet("/api/graph/overview")).nodes.filter(n => n.type === "Technology").map(n => ({id: n.id, name: n.label}));
    else if (type === "JobRole") items = (await apiGet("/api/graph/overview")).nodes.filter(n => n.type === "JobRole").map(n => ({id: n.id, name: n.label}));
    nodeSelect.innerHTML = items.map(i => `<option value="${i.id}">${i.name || i.title}</option>`).join("");
  } catch (e) { showToast(e.message); }
}
document.getElementById("explorerTypeSelect").addEventListener("change", loadExplorerNodeOptions);

async function exploreSelectedNode() {
  const type = document.getElementById("explorerTypeSelect").value;
  const id = document.getElementById("explorerNodeSelect").value;
  if (!id) return;
  exploreNode(type, id);
}

async function exploreNode(type, id) {
  const container = document.getElementById("cy-explorer");
  container.innerHTML = '';
  container.insertAdjacentHTML("beforebegin", '<div class="loading" id="explorerLoading"><div class="spinner"></div>Loading graph...</div>');
  try {
    const data = await apiGet(`/api/graph/${type}/${id}`);
    document.getElementById("explorerLoading")?.remove();
    cyExplorer = renderGraph("cy-explorer", data);
    renderLegend("explorerLegend");
  } catch (e) {
    document.getElementById("explorerLoading")?.remove();
    container.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
    showToast(e.message);
  }
}

async function loadExplorerOverview() {
  const container = document.getElementById("cy-explorer");
  container.innerHTML = '<div class="loading"><div class="spinner"></div>Loading graph...</div>';
  try {
    const data = await apiGet("/api/graph/overview");
    container.innerHTML = '';
    cyExplorer = renderGraph("cy-explorer", data);
    renderLegend("explorerLegend");
    document.getElementById("explorerInfoPanel").style.display = "none";
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div>${e.message}</div>`;
  }
}

// ---------------- Global search ----------------
let searchTimer = null;
document.getElementById("globalSearch").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  const box = document.getElementById("searchResults");
  if (!q) { box.classList.remove("show"); return; }
  searchTimer = setTimeout(async () => {
    try {
      const data = await apiGet("/api/search?q=" + encodeURIComponent(q));
      const groups = [
        {label: "People", items: data.people, onClick: p => openPersonDetail(p.id), name: p => p.name},
        {label: "Skills", items: data.skills, onClick: s => openSkillDetail(s.id), name: s => s.name},
        {label: "Projects", items: data.projects, onClick: p => openProjectDetail(p.id), name: p => p.name},
        {label: "Job Roles", items: data.jobRoles, onClick: null, name: r => r.title},
        {label: "Technologies", items: data.technologies, onClick: null, name: t => t.name},
      ].filter(g => g.items.length);
      if (!groups.length) { box.innerHTML = '<div class="search-result-item">No matches found.</div>'; box.classList.add("show"); return; }
      window.__searchGroups = groups;
      box.innerHTML = groups.map((g, gi) => `
        <div class="search-group-label">${g.label}</div>
        ${g.items.map((it, ii) => `<div class="search-result-item" data-gi="${gi}" data-ii="${ii}">${g.name(it)}</div>`).join("")}
      `).join("");
      box.querySelectorAll(".search-result-item[data-gi]").forEach(el => {
        el.addEventListener("click", () => {
          const g = window.__searchGroups[el.dataset.gi];
          const item = g.items[el.dataset.ii];
          if (g.onClick) g.onClick(item);
          box.classList.remove("show");
        });
      });
      box.classList.add("show");
    } catch (e) { showToast(e.message); }
  }, 250);
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".search-box")) document.getElementById("searchResults").classList.remove("show");
});

// ---------------- Detail overlay ----------------
function openDetailShell(loadingText) {
  document.getElementById("detailPanel").innerHTML = `<div class="loading"><div class="spinner"></div>${loadingText}</div>`;
  document.getElementById("detailOverlay").classList.add("show");
}
function closeDetail() { document.getElementById("detailOverlay").classList.remove("show"); }

// ---------------- Init ----------------
(async function init() {
  await loadStats();
  await loadOverviewGraph();
  // capture the cytoscape instance created for the dashboard graph
  cyDashboardInstance = window.__lastCy || null;
  loadDashboardCareerInsights();
})();

// Patch renderGraph to also stash the last created instance for zoom controls
const _origRenderGraph = renderGraph;
renderGraph = function(containerId, data) {
  const cy = _origRenderGraph(containerId, data);
  if (containerId === "cy-dashboard") cyDashboardInstance = cy;
  if (containerId === "cy-explorer") cyExplorer = cy;
  return cy;
};
</script>
</body>
</html>
"""


# ======================================================================
# 8. APPLICATION STARTUP
# ======================================================================
def main():
    parser = argparse.ArgumentParser(description="SkillGraph — Skill & Career Relationship Explorer")
    parser.add_argument("--seed", action="store_true", help="Seed CognoDB with sample data and exit.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    args = parser.parse_args()

    db.connect()

    if args.seed:
        if not db.is_connected:
            logger.error("Aborting seed: could not connect to CognoDB. Check your .env file.")
            sys.exit(1)
        ok = seed_database()
        db.close()
        sys.exit(0 if ok else 1)

    try:
        app.run(host="0.0.0.0", port=args.port, debug=False)
    finally:
        db.close()


if __name__ == "__main__":
    main()
