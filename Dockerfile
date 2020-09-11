FROM python:3.8.5-slim-buster

WORKDIR /srv/code
COPY openslides_backend openslides_backend
COPY requirements.txt .

RUN pip install --no-cache-dir --requirement requirements.txt

RUN adduser --system --no-create-home appuser

USER appuser

EXPOSE 9002
EXPOSE 9003

CMD [ "python", "-m", "openslides_backend" ]
