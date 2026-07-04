# Self-contained image — embedded DuckDB, no external services.
# Build:  docker build -t text2sql .
# Run:    docker run -p 8000:8000 -e GROQ_API_KEY=gsk_... text2sql
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python data/seed.py            # bake the sample warehouse into the image

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
