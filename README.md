# IntelligenceStack: Secure Agentic AI over Enterprise Lakehouses

![Databricks IntelligenceStack](./architecture_diagram.png)

## Overview
**IntelligenceStack** is a reference architecture and functional sandbox that demonstrates how to securely deploy Generative AI Agents over proprietary enterprise data. 

Rather than moving data to external AI providers, IntelligenceStack brings the AI to the data using the **Databricks Data Intelligence Platform**. It provides a fully functional control plane, synthetic data generator, and cognitive agent that proves out secure, governed, and high-performance AI integration.

## Features
- **Streaming Ingestion**: Simulates massive real-time event streaming using Databricks Auto Loader principles.
- **Cognitive Agent Core**: An LLM orchestrator that translates natural language intent into structured analytical tool execution.
- **Governed Boundary**: Simulates Unity Catalog UDF restrictions, proving that the Agent cannot execute arbitrary SQL and is bound to strict role-based access.
- **Interactive Control Plane**: A sleek, premium Streamlit dashboard featuring live telemetry, real-time latency tracking, and interactive Agent Trace Route visualization.

## Technical Architecture (Sandbox)
- **`src/api/app.py`**: FastAPI backend hosting the Lakehouse Agent API.
- **`src/api/ui.py`**: The Streamlit IntelligenceStack Control Plane.
- **`src/cognitive/agent_core.py`**: The intent parser and Databricks execution simulator.
- **`src/ingestion/synthetic_generator.py`**: A continuous Python process using `Faker` to generate raw streaming JSON logs into the simulated landing zone.

## Quickstart Guide

### 1. Environment Setup
Initialize the Python virtual environment and install the required dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Start the Synthetic Data Stream
In a new terminal window, activate the environment and start the synthetic data generator. This will continuously write JSON batches to `mnt/telemetry/raw_logs`, simulating real enterprise traffic.
```bash
source venv/bin/activate
python src/ingestion/synthetic_generator.py
```

### 3. Launch the Backend API
In a new terminal window, start the FastAPI Core Engine:
```bash
source venv/bin/activate
PYTHONPATH=. python src/api/app.py
```

### 4. Launch the IntelligenceStack Control Plane
In your final terminal window, launch the interactive UI dashboard:
```bash
source venv/bin/activate
PYTHONPATH=. streamlit run src/api/ui.py --server.port 8501
```

Visit `http://localhost:8501` in your browser to interact with the agent, view the real-time telemetry dashboard, and inspect the system architecture.

## Further Reading
Please review the [EXECUTIVE_SUMMARY.md](./EXECUTIVE_SUMMARY.md) for a deep dive into the business significance and architectural mapping of IntelligenceStack to a production Databricks environment.
