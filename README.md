# FinCoach — Smart Personal Finance Ledger & AI Coach

A personal finance app that reconciles your bank statements (ICICI, HDFC, SBI, Bank of Baroda) with UPI app histories (PhonePe, Google Pay), auto-categorizes transactions using AI, tracks budgets with run-rate alerts, and provides AI-powered savings coaching.

## Architecture

```
frontend/          → Next.js 14 PWA (TypeScript + Tailwind)
backend/           → FastAPI (Python 3.12 + SQLAlchemy + pdfplumber)
docker-compose.yml → Postgres 16 + pgvector
```

## Quick Start

### 1. Start Postgres
```bash
docker compose up -d
```

### 2. Backend
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit with your keys
uvicorn app.main:app --reload --port 8000
```

### 3. Frontend
```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000

## Key Features
- **Upload** bank PDFs/CSVs + UPI app exports
- **UTR-based reconciliation** (bank ↔ UPI app matching)
- **3-tier AI categorization**: Rules → Embeddings → LLM (cached per merchant)
- **Budget tracking** with adaptive baselines & run-rate projections
- **Recurring subscription detection**
- **AI finance coach** (GPT-4o-mini powered insights)
- **Encrypted file storage** for uploaded statements
- **Dark-mode PWA** — installable on mobile

## Tech Stack
| Layer | Tech |
|-------|------|
| Frontend | Next.js 14, TypeScript, Tailwind CSS, Recharts, Lucide Icons |
| Backend | FastAPI, SQLAlchemy 2.0, Pydantic v2, pdfplumber |
| Database | PostgreSQL 16 + pgvector |
| AI | OpenAI / OpenRouter gpt-4o-mini + text-embedding-3-small |
| Security | Fernet encryption at rest, SHA-256 dedup |

