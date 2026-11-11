FROM python:3.11-slim

WORKDIR /app

COPY generate_site.py serve.py ./
RUN python generate_site.py

EXPOSE 80
CMD ["python", "-u", "serve.py"]
