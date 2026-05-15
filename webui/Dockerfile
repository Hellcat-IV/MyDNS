FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5380
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5380", "--access-logfile", "-", "app:create_app()"]
