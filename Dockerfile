FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt reportlab
COPY . .
EXPOSE 8000
# Migrations run at start-up (SCOS_AUTO_MIGRATE=1). Load the demo once with:
#   docker compose run --rm app sh -c "python -m seed.make_samples && python -m seed.demo_seed"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
