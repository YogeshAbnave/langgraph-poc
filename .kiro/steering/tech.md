# Technology Stack

## Core Framework

- **Python**: 3.10+
- **LangGraph**: Agent orchestration and graph-based reasoning
- **LangChain**: LLM integration and tooling
- **FastAPI**: Web framework (via bedrock-agentcore)

## AWS Services

- **AWS Bedrock**: LLM hosting and inference
- **Bedrock AgentCore**: Agent deployment and runtime management

## Key Libraries

- `langgraph`: Graph-based agent framework
- `langchain-aws`: AWS Bedrock integration
- `langchain_community`: Community tools (DuckDuckGo search)
- `bedrock-agentcore`: Runtime framework
- `bedrock-agentcore-starter-toolkit`: CLI tools for deployment
- `duckduckgo-search`: Web search functionality
- `opentelemetry-instrumentation-langchain`: Observability

## Package Management

- **uv**: Fast Python package installer and resolver (preferred)
- Alternative: pip

## LLM Model

- **Model**: Anthropic Claude 3 Haiku (`global.anthropic.claude-haiku-4-5-20251001-v1:0`)
- **Provider**: AWS Bedrock Converse API

## Common Commands

### Environment Setup
```bash
# Install uv
pip install uv

# Create virtual environment
uv venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
uv pip install -r requirements.txt
```

### Deployment (AgentCore Toolkit)
```bash
# Configure agent for deployment
agentcore configure

# Deploy agent
agentcore launch -e langgraph_agent_web_search.py

# Test deployed agent
agentcore invoke {"prompt":"Your query here"}

# Remove deployment
agentcore destroy
```

### Local Development
```bash
# Run agent locally (if applicable)
python langgraph_agent_web_search.py
```

## Configuration

- **LangSmith**: OpenTelemetry enabled via `LANGSMITH_OTEL_ENABLED` environment variable
- **Logging**: LangChain debug logging enabled
- **AWS Credentials**: Required for Bedrock access (configured via AWS CLI or environment variables)
