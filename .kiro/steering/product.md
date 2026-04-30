# Product Overview

This is a LangGraph-based AI agent with web search capabilities, integrated with AWS Bedrock AgentCore for managed deployment.

## Core Functionality

- Synchronous agent that processes user queries
- Web search integration using DuckDuckGo
- Uses Anthropic Claude 3 Haiku model via AWS Bedrock
- Deployed as a managed service on AWS

## Key Features

- Graph-based reasoning flow using LangGraph
- Tool-calling capabilities for web search
- AWS Bedrock AgentCore integration for deployment and scaling
- OpenTelemetry instrumentation for observability

## Use Cases

The agent can answer questions requiring current information by:
1. Receiving user queries
2. Determining if web search is needed
3. Executing searches via DuckDuckGo
4. Synthesizing responses from search results
