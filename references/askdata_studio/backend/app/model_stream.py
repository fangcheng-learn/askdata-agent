"""OpenAI-compatible SSE aggregation; incomplete JSON is never executable."""
import json
import time


class ChatStream:
    def __init__(self):
        self.raw = []
        self.content = []
        self.reasoning = []
        self.usage = None
        self.finish_reason = None
        self.done = False

    def read(self, response, *, deadline, check=lambda: None):
        data = []
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Model stream wall-clock deadline reached')
            # urllib uses a socket timeout, not a whole-response deadline.
            # Bound each blocking read by the SAME absolute deadline.
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                sock.settimeout(remaining)
            line = response.readline()
            if not line:
                raise ConnectionError('Model stream ended before [DONE]')
            self.raw.append(line)
            if time.monotonic() >= deadline:
                raise TimeoutError('Model stream wall-clock deadline reached')
            line = line.decode('utf-8').rstrip('\r\n')
            if line.startswith('data:'):
                data.append(line[5:].lstrip())
            elif not line and data:
                payload = '\n'.join(data)
                data = []
                if payload == '[DONE]':
                    self.done = True
                    if self.finish_reason != 'stop' or not self.content:
                        raise ConnectionError('Model stream did not finish a complete answer')
                    return {'choices': [{'message': {'content': ''.join(self.content),
                        'reasoning_content': ''.join(self.reasoning)}, 'finish_reason': self.finish_reason}],
                        'usage': self.usage}
                event = json.loads(payload)
                if event.get('error'):
                    raise ConnectionError('Model stream returned an error event')
                if event.get('usage') is not None:
                    self.usage = event['usage']
                for choice in event.get('choices') or []:
                    if choice.get('index', 0) != 0:
                        continue
                    delta = choice.get('delta') or {}
                    if delta.get('content'):
                        self.content.append(delta['content'])
                    if delta.get('reasoning_content'):
                        self.reasoning.append(delta['reasoning_content'])
                    if choice.get('finish_reason'):
                        self.finish_reason = choice['finish_reason']
