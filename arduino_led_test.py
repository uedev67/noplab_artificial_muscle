"""아두이노 LED 마커에 시리얼 명령을 보내는 독립 테스트.

실행: python arduino_led_test.py
포트 목록: python arduino_led_test.py --list-ports
설정 변경: python arduino_led_test.py --port COM9 --baudrate 9600 --command start

아두이노에는 해당 문자열을 받아 LED를 켜는 스케치가 업로드되어 있어야 합니다.
PC의 전송 성공만으로 LED 점등을 확인할 수는 없습니다.
"""

import argparse
import sys
import time


# 기존 실험의 마커 설정과 동일합니다. 아두이노 스케치와 맞춰 주세요.
PORT = "COM9"
BAUDRATE = 9600
COMMAND = "start"
LINE_ENDING = "lf"  # lf: \n, crlf: \r\n, none: 줄바꿈 없음
READY_DELAY_S = 2.0
RESPONSE_WAIT_S = 3.0


def read_responses(connection, duration):
    """지정된 시간 동안 수신한 원시 바이트를 표시합니다."""
    deadline = time.monotonic() + duration
    received = False
    while time.monotonic() < deadline:
        data = connection.read(connection.in_waiting or 1)
        if data:
            print(f"RX: {data!r}")
            received = True
    return received


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=PORT)
    parser.add_argument("--baudrate", type=int, default=BAUDRATE)
    parser.add_argument("--command", default=COMMAND)
    parser.add_argument("--line-ending", choices=("lf", "crlf", "none"), default=LINE_ENDING)
    parser.add_argument("--list-ports", action="store_true")
    args = parser.parse_args()

    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        print("pyserial이 필요합니다. 현재 Python에 다음 명령으로 설치하세요:")
        print(f'"{sys.executable}" -m pip install pyserial')
        return 1

    print(f"Python: {sys.executable}")
    ports = list(list_ports.comports())
    print("인식된 시리얼 포트:")
    for port in ports:
        print(f"  {port.device}: {port.description} [{port.hwid}]")
    if not ports:
        print("  없음")
    if args.list_ports:
        return 0

    ending = {"lf": "\n", "crlf": "\r\n", "none": ""}[args.line_ending]
    payload = (args.command + ending).encode("utf-8")
    try:
        with serial.Serial(args.port, args.baudrate, timeout=0.1, write_timeout=2.0) as connection:
            print(f"연결됨: {args.port}, {args.baudrate} baud")
            print(f"아두이노 준비 대기 {READY_DELAY_S}초 (수신 내용도 표시합니다).")
            read_responses(connection, READY_DELAY_S)
            print(f"전송할 명령: {payload!r}")
            print("Enter: 명령 전송 / q + Enter: 종료")
            while True:
                choice = input("> ").strip().lower()
                if choice == "q":
                    break
                if choice:
                    print("전송하려면 Enter, 종료하려면 q를 입력하세요.")
                    continue
                written = connection.write(payload)
                if written != len(payload):
                    raise OSError(f"일부만 전송됨: {written}/{len(payload)} bytes")
                connection.flush()
                print(f"TX: {payload!r} ({written} bytes); LED 점등을 직접 확인하세요.")
                if not read_responses(connection, RESPONSE_WAIT_S):
                    print("수신 응답 없음. 스케치에 응답 코드가 없으면 정상일 수도 있습니다.")
    except (serial.SerialException, OSError) as exc:
        print(f"시리얼 오류: {exc}")
        print("포트 번호와 USB 연결을 확인하고, Arduino 시리얼 모니터 및 포트를 사용하는 다른 프로그램을 닫으세요.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n테스트 종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
