import os
import json
import sqlite3
import urllib.request
from pydantic import BaseModel, Field, ValidationError
from typing import List, Dict, Any, Optional
from flask import Flask, request, jsonify

# --- Initialize Flask App ---
app = Flask(__name__)

# --- Configuration ---
# NOTE: These variables MUST be set as Environment Variables on Render's dashboard!
MODEL_NAME = "mistralai/mistral-7b-instruct:free"

# Global State and Database Setup
# Using in-memory SQLite for simplicity, initialized once at startup.
# In a persistent server (Render Web Service), this state is maintained.
CHAT_HISTORY: Dict[str, List[Dict[str, str]]] = {}
DB_CONNECTION: Optional[sqlite3.Connection] = None

# Database Schema - DDL 
INVENTORY_DB_SCHEMA = """
CREATE TABLE Customers (CustomerId INTEGER PRIMARY KEY, CustomerName TEXT);
CREATE TABLE Vendors (VendorId INTEGER PRIMARY KEY, VendorName TEXT);
CREATE TABLE Sites (SiteId INTEGER PRIMARY KEY, SiteName TEXT);
CREATE TABLE Locations (LocationId INTEGER PRIMARY KEY, SiteId INTEGER, LocationCode TEXT, LocationName TEXT);
CREATE TABLE Items (ItemId INTEGER PRIMARY KEY, ItemCode TEXT UNIQUE, ItemName TEXT, Category TEXT);
CREATE TABLE Assets (AssetId INTEGER PRIMARY KEY, ItemId INTEGER, SiteId INTEGER, LocationId INTEGER, Status TEXT, PurchaseDate TEXT, Cost REAL, VendorId INTEGER);
CREATE TABLE Bills (BillId INTEGER PRIMARY KEY, VendorId INTEGER, BillNumber VARCHAR(100), BillDate DATE, TotalAmount REAL, Status VARCHAR(30));
CREATE TABLE PurchaseOrders (POId INTEGER PRIMARY KEY, VendorId INTEGER, PONumber VARCHAR(100), PODate DATE, Status VARCHAR(30), SiteId INTEGER);
CREATE TABLE SalesOrders (SOId INTEGER PRIMARY KEY, CustomerId INTEGER, SONumber VARCHAR(100), SODate DATE, Status VARCHAR(30), SiteId INTEGER);
"""

# PYDANTIC MODELS (Used for validation)
class ChatRequest(BaseModel):
    session_id: str = Field(description="Unique identifier for the user's session.")
    message: str = Field(description="The user's natural language question.")
    context: Dict[str, Any] = Field(default_factory=dict, description="Optional context object.")

class SQLResponse(BaseModel):
    sql_query: str = Field(description="The exact SQL query generated to answer the question, adhering strictly to the schema.")
    natural_language_answer_template: str = Field(description="The final answer template in English, which includes '{value}' as a placeholder for the SQL query execution result.")

class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatResponse(BaseModel):
    natural_language_answer: str = Field(description="The final, complete answer in English natural language (after value interpolation).")
    sql_query: str = Field(description="The generated SQL query that was executed (The 'Present Query').")
    token_usage: TokenUsage


def setup_mock_db():
    """Initializes the in-memory SQLite database and populates mock data."""
    global DB_CONNECTION
    if DB_CONNECTION is None:
        DB_CONNECTION = sqlite3.connect(':memory:')
        cursor = DB_CONNECTION.cursor()
        
        # Run the DDL
        for stmt in INVENTORY_DB_SCHEMA.split(';'):
            if stmt.strip():
                try:
                    cursor.execute(stmt)
                except sqlite3.OperationalError:
                    pass
                
        mock_data = [
            ("INSERT INTO Sites VALUES (1, 'New York Warehouse');"),
            ("INSERT INTO Sites VALUES (2, 'Los Angeles Depot');"),
            ("INSERT INTO Items VALUES (1, 'LAP101', 'Laptop Model A', 'Electronics');"),
            ("INSERT INTO Items VALUES (2, 'MOU05', 'Wireless Mouse', 'Accessories');"),
            ("INSERT INTO Assets VALUES (1, 1, 1, NULL, 'Active', '2023-01-15', 1200.00, NULL);"),
            ("INSERT INTO Assets VALUES (2, 1, 1, NULL, 'Active', '2023-02-20', 1200.00, NULL);"),
            ("INSERT INTO Assets VALUES (3, 2, 2, NULL, 'Active', '2024-03-01', 25.00, NULL);"),
            ("INSERT INTO Assets VALUES (4, 1, 2, NULL, 'Disposed', '2023-04-05', 1200.00, NULL);"),
        ]
        for sql in mock_data:
            try:
                cursor.execute(sql)
            except:
                pass
        
        DB_CONNECTION.commit()
    return DB_CONNECTION

def execute_sql(conn: sqlite3.Connection, sql_query: str):
    """Executes the SQL query against the in-memory database."""
    # ... (Same execute_sql logic as before) ...
    try:
        cursor = conn.cursor()
        if not sql_query.strip().upper().startswith("SELECT"):
            return "Execution Error: Only SELECT queries are permitted.", None
            
        cursor.execute(sql_query)
        rows = cursor.fetchall()
        
        if not rows:
            return "No data found.", None
            
        if len(rows[0]) == 1 and len(rows) == 1:
            return str(rows[0][0]), None
        else:
            columns = [col[0] for col in cursor.description]
            response_text = "Result Table:\n" 
            response_text += "| " + " | ".join(columns) + " |\n"
            response_text += "|-" + "-|-".join(["-" * len(c) for c in columns]) + "-|\n"
            for row in rows:
                response_text += "| " + " | ".join(map(str, row)) + " |\n"
            return response_text, None

    except sqlite3.Error as e:
        return f"Database Error: {e}", None
    except Exception as e:
        return f"Unexpected Execution Error: {e}", None

def _get_llm_endpoint_config():
    """Configures the OpenRouter API endpoint using environment variable."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    api_key = os.environ.get("OPENROUTER_API_KEY")
    
    if not api_key:
        raise EnvironmentError("OPENROUTER_API_KEY environment variable is not set on Render.")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8000") # Use Render's environment variable
    }
    
    return url, headers

def _build_system_prompt(model: BaseModel) -> str:
    # ... (Same system prompt logic as before) ...
    schema_json = json.dumps(model.model_json_schema(), indent=2)
    
    full_schema_from_pdf = """
    CREATE TABLE Customers (Customerld INT PRIMARY KEY, CustomerCode VARCHAR(50), CustomerName NVARCHAR(200), IsActive BIT);
    CREATE TABLE Vendors (Vendorld INT PRIMARY KEY, VendorCode VARCHAR(50), VendorName NVARCHAR(200), IsActive BIT);
    CREATE TABLE Sites (Siteld INT PRIMARY KEY, SiteCode VARCHAR(50), SiteName NVARCHAR(200), IsActive BIT);
    CREATE TABLE Locations (LocationId INT PRIMARY KEY, Siteld INT, LocationCode VARCHAR(50), LocationName NVARCHAR(200));
    CREATE TABLE Items (Itemld INT PRIMARY KEY, ItemCode NVARCHAR(100), ItemName NVARCHAR(200), Category NVARCHAR(100));
    CREATE TABLE Assets (Assetld INT PRIMARY KEY, AssetTag VARCHAR(100), AssetName NVARCHAR(200), Siteld INT, Locationld INT, SerialNumber NVARCHAR(200), Category NVARCHAR(100), Status VARCHAR(30), Cost DECIMAL(18,2), PurchaseDate DATE, Vendorld INT);
    CREATE TABLE Bills (Billld INT PRIMARY KEY, Vendorld INT, BillNumber VARCHAR(100), BillDate DATE, TotalAmount DECIMAL(18,2), Status VARCHAR(30));
    CREATE TABLE PurchaseOrders (POID INT PRIMARY KEY, PONumber VARCHAR(100), Vendorld INT, PODate DATE, Status VARCHAR(30), Siteld INT);
    CREATE TABLE SalesOrders (SOID INT PRIMARY KEY, SONumber VARCHAR(100), Customerld INT, SODate DATE, Status VARCHAR(30), Siteld INT);
    """
    
    prompt = f"""
    You are a specialized Inventory and ERP chatbot. Your role is to convert user queries into precise SQL queries, execute them (via a placeholder), and return a natural language answer.

    1. Your Task: Generate a single, accurate SQL query from the user's question.
    2. Database Schema: Use the following SQL schema to generate the query. Be meticulous about field names and relationships:
    --- START SCHEMA ---
    {full_schema_from_pdf}
    --- END SCHEMA ---

    3. Output Format: Your completion MUST be a single JSON object that perfectly matches the Pydantic model below. The response MUST use **ENGLISH** for the 'natural_language_answer_template'.
    {schema_json}

    4. Special Instructions:
       - The SQL query must be syntactically correct and executable on SQLite.
       - The 'natural_language_answer_template' field MUST be in **ENGLISH** and MUST contain the variable '{{value}}'.
       - Use the conversation history to maintain context for follow-up questions.
       - Refusal Policy: If the user's question CANNOT be answered using the provided SQL schema, you MUST politely decline the request, stating clearly that you are an Inventory Specialist Chatbot and can only answer questions related to the provided tables.
    """
    return prompt

def _call_llm_for_sql(session_id: str, message: str) -> SQLResponse:
    """Calls the OpenRouter LLM to generate SQL and a response template."""
    url, headers = _get_llm_endpoint_config()
    history = CHAT_HISTORY.setdefault(session_id, [])

    if not history or history[0]["role"] != "system":
        system_prompt = _build_system_prompt(SQLResponse)
        history.insert(0, {"role": "system", "content": system_prompt})
    
    # Add user message to history temporarily
    history.append({"role": "user", "content": message})
    
    payload = {
        "model": MODEL_NAME, 
        "messages": history,
        "temperature": 0.0,
        "response_format": {"type": "json_object"} 
    }
    
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        
        with urllib.request.urlopen(req) as response:
            if response.status != 200:
                error_message = response.read().decode('utf-8')
                raise Exception(f"API Error: Status {response.status}, Details: {error_message}")
                
            response_json = json.loads(response.read().decode("utf-8"))
            
            llm_content = response_json["choices"][0]["message"]["content"]
            usage = response_json.get("usage", {})
            
            llm_response = SQLResponse.model_validate_json(llm_content)
            
            return llm_response, TokenUsage(**usage)
            
    except Exception as e:
        # Remove the user message from history if the LLM call or JSON parsing fails.
        if history and history[-1].get("role") == "user":
            history.pop() 
        raise Exception(f"LLM Call Error or JSON Validation Failed: {e}")


@app.route('/api/chat', methods=['POST'])
def chat_endpoint():
    # 1. Setup DB (guaranteed to be initialized once for a persistent server)
    conn = setup_mock_db()
    
    try:
        # 2. Validate input using Pydantic
        data = request.get_json()
        chat_request = ChatRequest(**data)
        
        session_id = chat_request.session_id
        current_history = CHAT_HISTORY.setdefault(session_id, [])
        
        # 3. Call LLM (user message added to history inside _call_llm_for_sql)
        llm_response, usage = _call_llm_for_sql(session_id, chat_request.message)
        
        # 4. Execute SQL
        sql_result, exec_error = execute_sql(conn, llm_response.sql_query)
        
        # 5. History and Response Management (Fix for Repetition Bug)
        is_refusal_or_error = exec_error or "sorry" in llm_response.natural_language_answer_template.lower()
        
        if is_refusal_or_error:
            # --- FAILURE CASE ---
            
            # Remove the user message that caused the failure from history
            if current_history and current_history[-1].get("role") == "user":
                current_history.pop() 
                
            # Determine the final error message
            if exec_error:
                final_answer = "I'm sorry, the generated query failed to execute against the database. Please try rephrasing your inventory question."
                final_sql = f"Execution Error: {sql_result}"
            else:
                # Model refusal
                final_answer = llm_response.natural_language_answer_template.format(value="").strip() 
                final_sql = f"Refused: Query is outside the scope of Inventory data."
            
        else:
            # --- SUCCESS CASE ---
            
            # Save the successful assistant response to history
            current_history.append({"role": "assistant", "content": f"Query: {llm_response.sql_query}. Answer Template: {llm_response.natural_language_answer_template}"})
            
            # Generate the final natural language answer
            try:
                final_answer = llm_response.natural_language_answer_template.format(value=sql_result)
            except KeyError:
                final_answer = f"Generated Answer Template: {llm_response.natural_language_answer_template} | Query Result Value: {sql_result}"
            final_sql = llm_response.sql_query

        final_response = ChatResponse(
            natural_language_answer=final_answer,
            sql_query=final_sql,
            token_usage=usage
        )

        return jsonify(final_response.model_dump())

    except ValidationError as e:
        return jsonify({"error": "Invalid Input", "details": e.errors()}), 400
    except Exception as e:
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


@app.route('/', methods=['GET'])
def index():
    # Serve the HTML UI at the root path
    HTML_UI = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Inventory Chatbot (Render Deployment)</title>
        <style>
            body { font-family: Tahoma, sans-serif; background-color: #f4f4f9; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
            .chat-container { width: 90%; max-width: 700px; background: white; border-radius: 10px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1); display: flex; flex-direction: column; overflow: hidden; }
            .chat-header { background-color: #28a745; color: white; padding: 15px; text-align: center; font-size: 1.2em; }
            .chat-box { flex-grow: 1; padding: 20px; max-height: 500px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; }
            .user-message, .bot-message { padding: 10px 15px; border-radius: 15px; max-width: 80%; }
            .user-message { background-color: #d1e7ff; align-self: flex-end; }
            .bot-message { background-color: #e9ecef; align-self: flex-start; }
            .sql-query { margin-top: 10px; font-size: 0.8em; color: #555; background-color: #f8f9fa; padding: 8px; border-radius: 5px; white-space: pre-wrap; word-break: break-all; text-align: left; }
            .input-area { display: flex; padding: 15px; border-top: 1px solid #ddd; }
            .input-area input { flex-grow: 1; padding: 10px; border: 1px solid #ccc; border-radius: 20px; margin-right: 10px; text-align: left; }
            .input-area button { padding: 10px 20px; background-color: #28a745; color: white; border: none; border-radius: 20px; cursor: pointer; }
            .token-info { font-size: 0.7em; color: #999; margin-top: 5px; text-align: left; }
        </style>
    </head>
    <body>
        <div class="chat-container">
            <div class="chat-header">
                Inventory Chatbot (Render Deployment)
            </div>
            <div class="chat-box" id="chatBox">
                <div class="bot-message">
                    Hello! I am your Inventory Chatbot. Ask me questions about your assets, purchases, or sales orders.
                    <div class="sql-query">
                        <p><strong>IMPORTANT:</strong> This server maintains conversation history across requests.</p>
                    </div>
                </div>
            </div>
            <div class="input-area">
                <input type="text" id="userInput" placeholder="Type your question here..." onkeypress="if(event.key === 'Enter') sendMessage()">
                <button onclick="sendMessage()">Send</button>
            </div>
        </div>

        <script>
            const SESSION_ID = 'session_' + Math.random().toString(36).substring(2, 9);
            const CHAT_API_URL = '/api/chat'; 

            function displayMessage(role, text, sqlQuery = null, usage = null) {
                const chatBox = document.getElementById('chatBox');
                const messageDiv = document.createElement('div');
                messageDiv.className = role === 'user' ? 'user-message' : 'bot-message';
                messageDiv.innerText = text;

                if (sqlQuery) {
                    const sqlDiv = document.createElement('div');
                    sqlDiv.className = 'sql-query';
                    sqlDiv.innerHTML = \`<strong>Present Query (SQL):</strong><br>\${sqlQuery.trim()}\`;
                    messageDiv.appendChild(sqlDiv);
                }
                
                if (usage) {
                    const tokenDiv = document.createElement('div');
                    tokenDiv.className = 'token-info';
                    tokenDiv.innerText = \`Tokens: P:\${usage.prompt_tokens} | C:\${usage.completion_tokens}\`;
                    messageDiv.appendChild(tokenDiv);
                }
                
                chatBox.appendChild(messageDiv);
                chatBox.scrollTop = chatBox.scrollHeight;
            }

            async function sendMessage() {
                const inputElement = document.getElementById('userInput');
                const message = inputElement.value.trim();
                if (!message) return;

                displayMessage('user', message);
                inputElement.value = '';

                try {
                    const response = await fetch(CHAT_API_URL, { 
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({
                            session_id: SESSION_ID,
                            message: message,
                            context: {}
                        })
                    });

                    if (!response.ok) {
                        const error_data = await response.json();
                        let error_msg = error_data.error || response.statusText;
                        displayMessage('bot', \`Server Error: \${error_msg}. Check Render logs.\`);
                        return;
                    }

                    const data = await response.json();
                    
                    displayMessage(
                        'bot',
                        data.natural_language_answer,
                        data.sql_query,
                        data.token_usage
                    );

                } catch (error) {
                    console.error('Fetch error:', error);
                    displayMessage('bot', \`Connection Failed: Ensure the Render service is active.\`);
                }
            }
        </script>
    </body>
    </html>
    """
    return index_html

# The index() function serves the HTML, which needs to be defined globally 
# for the route handler. We use a simple placeholder here.
index_html = """
<!DOCTYPE html>
<html lang="en">
</html>
"""

# Call setup_mock_db once to initialize the DB when the app starts
setup_mock_db()

# If you run locally for testing (python api/chat.py), uncomment this block
# if __name__ == '__main__':
#     app.run(debug=True, host='0.0.0.0', port=os.environ.get('PORT', 8080))