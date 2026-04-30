# Project Structure

## Root Directory

```
.
├── langgraph_agent_web_search.py  # Main agent implementation
├── requirements.txt                # Python dependencies
└── README.md                       # Documentation
```

## File Descriptions

### `langgraph_agent_web_search.py`
Main agent implementation file containing:
- **LLM Initialization**: Bedrock Converse API setup with Claude Haiku
- **Tool Definition**: DuckDuckGo search tool configuration
- **State Management**: TypedDict-based state for message handling
- **Graph Construction**: LangGraph nodes and edges for agent flow
- **AgentCore Integration**: Entrypoint decorator and app runtime

### `requirements.txt`
Python package dependencies for the project

### `README.md`
Comprehensive documentation including setup, usage, and deployment instructions

## Code Organization Patterns

### Agent Flow Structure
1. **State Definition**: TypedDict with annotated message list
2. **Chatbot Node**: Processes messages and decides on tool usage
3. **Tool Node**: Executes web search when needed
4. **Conditional Edges**: Routes between chatbot and tools based on LLM decision
5. **Entrypoint**: BedrockAgentCore handler that wraps the graph

### Key Components
- **Graph Builder**: `StateGraph(State)` - defines agent workflow
- **Nodes**: `chatbot` (LLM reasoning), `tools` (search execution)
- **Edges**: `START → chatbot`, `chatbot ↔ tools` (conditional)
- **Compilation**: `graph.compile()` - creates executable graph

## Conventions

- Single-file implementation for simplicity
- Print statements for debugging and startup logging
- Environment variables for configuration (e.g., `LANGSMITH_OTEL_ENABLED`)
- Payload structure: `{"prompt": "user query"}` for input
- Response structure: `{"result": "agent response"}` for output
