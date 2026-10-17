FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Bake the seeded dataset + DuckDB star schema into the image so the
# dashboard is fully self-contained (deterministic: same seed, same data).
RUN python -m data_gen.generate && python -m pipeline.etl

EXPOSE 8501

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
