# MLOps

A curated collection of hands-on guides, tutorials, and reference implementations for **Machine Learning Operations (MLOps)** — covering topics like observability, model deployment, monitoring, automation, scalable architecture, AI Governance and scalable infrastructure.

Whether you're an **MLOps engineer**, **data scientist**, or **AI enthusiast**, this repo is designed to help you build, ship, and manage ML systems in production.

## Table of Content

* **vLLM**
  * [vLLM overview](vLLM)
  * [vLLM observability](vLLM/observability)


# Example Sankey Diagram about app -> api-key -> model Observability

```mermaid
sankey-beta
app-rag,app-rag/production,120
app-rag,app-rag/staging,45
app-etl,app-etl/production,60
free,free/client-a,15
app-rag/production,Qwen2.5-0.5B-Instruct,80
app-rag/production,Llama-3.1-8B-Instruct,40
app-rag/staging,Qwen2.5-0.5B-Instruct,45
app-etl/production,Llama-3.1-8B-Instruct,60
free/client-a,Qwen2.5-0.5B-Instruct,15
```


## Author

Created by [Himadri Talukder](https://www.linkedin.com/in/himadri-talukder-214b2539/)
If you find this helpful, please ⭐ the repo and share it with your MLOps community!
