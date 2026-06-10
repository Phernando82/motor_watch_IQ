import os
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient

load_dotenv()

url   = os.getenv('INFLUX_URL')
token = os.getenv('INFLUX_TOKEN')
org   = os.getenv('INFLUX_ORG')

print(f'URL:  {url}')
print(f'ORG:  {org}')
print(f'TOKEN presente: {bool(token)}')

client = InfluxDBClient(url=url, token=token, org=org)

health = client.health()
print(f'Health: {health.status}')

query = """
from(bucket: "motors")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "motor_telemetry")
  |> filter(fn: (r) => r.motor_id == "1")
  |> filter(fn: (r) => r._field == "vrms_magnitude_mms")
  |> last()
"""

tables = client.query_api().query(query, org=org)
found = False
for table in tables:
    for record in table.records:
        print(f'Motor 1 vrms last: {record.get_value():.3f} mm/s @ {record.get_time()}')
        found = True

if not found:
    print('Sem dados nos últimos 5min — inicia o Simulator em AUTO e tenta de novo')

client.close()
print('InfluxDB OK')