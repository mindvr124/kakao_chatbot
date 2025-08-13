from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI
client = OpenAI()

response = client.responses.create(
    model="gpt-4o-mini",
    input="안녕 난 홍길동이야!",
)
print(response.output_text)

second_response = client.responses.create(
    model="gpt-4o-mini",
    previous_response_id=response.id,
    input=[{"role": "user", "content": "대한민국의 수도는 어디야?"}],
)
print(second_response.output_text)

third_response = client.responses.create(
    model="gpt-4o-mini",
    previous_response_id=second_response.id,
    input=[{"role": "user", "content": "내 이름을 기억해?"}],
)
print(third_response.output_text)
