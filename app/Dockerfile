# Usamos uma versão "slim" para economizar RAM e espaço
FROM python:3.11-slim

# Impede que o Python grave arquivos .pyc e força o log a aparecer no terminal na hora
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copia os requerimentos e instala
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do nosso código
COPY . .

# Expõe a porta interna que o FastAPI vai usar
EXPOSE 8000

# O comando que inicia o Dr. Occam
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
