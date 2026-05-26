FROM python:3.12-slim

WORKDIR /app

# Installation des dépendances système légères
RUN apt-get update && apt-get install -y --no-install-recommends \
   curl \
   && rm -rf /var/lib/apt/lists/*

# Copie et installation des prérequis Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du code d'application
COPY configuration.py .
COPY listener.py .

# Exécution non-root pour plus de sécurité
USER 10001:10001

ENTRYPOINT ["python", "listener.py"]
