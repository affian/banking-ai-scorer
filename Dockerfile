FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Non-root user compliance for enterprise banking container security
RUN useradd -u 1001 appuser && chown -R appuser:appuser /app
USER 1001

EXPOSE 8080
ENV PORT=8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
