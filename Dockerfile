FROM python:3.11-slim
WORKDIR /app
COPY requirements_licencias.txt .
RUN pip install --no-cache-dir -r requirements_licencias.txt
COPY servidor_licencias.py ./
CMD ["python", "servidor_licencias.py"]
