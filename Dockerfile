FROM python:3.11

WORKDIR /app

COPY . /app

RUN pip install -r requirements.txt

RUN chmod +x start.sh

EXPOSE 8001
EXPOSE 8004

CMD [ "./start.sh" ]