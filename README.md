# Keithley 인공근육 실험 프로젝트

Python 3.10과 Keithley 2450을 기준으로 만든 터미널 기반 전압 sweep 실험 프로젝트입니다.
장비가 없을 때는 기본값인 더미 모드로 데이터 기록 전 과정을 확인할 수 있습니다.

## 파일 역할

- `keithley_driver.py`: 공통 SMU 인터페이스, Keithley 2450 PyVISA 드라이버, 더미 SMU
- `displacement_sensor.py`: 변위 센서 인터페이스, 더미 센서, 실제 센서 구현 자리
- `data_logger.py`: 시간과 전기/변위/비율 데이터를 UTF-8 CSV로 기록
- `experiment.py`: 전압 sweep, 동기화된 샘플링, 비율 계산, 안전 종료 수행
- `controller.py`: 추후 폐루프 실험에 사용할 P/PI/PID 제어기
- `plot_data.py`: CSV에서 세 가지 관계 그래프 생성
- `requirements.txt`: Python 3.10 호환 의존성
- `environment.yml`: 이름이 `noplab`인 공용 Conda 환경 정의
- `setup_windows.ps1`: Conda 환경 및 `noplab` PowerShell 명령 자동 설정

CSV 열은 `time`, `voltage`, `current`, `resistance`, `displacement`,
`contraction_ratio`, `resistance_change_ratio` 순서입니다. 수축률과 저항변화율의
단위는 `%`, 변위와 초기 길이의 단위는 `mm`입니다. 변위는 수축 방향을 양수로 봅니다.

## Windows 최초 설치

이 저장소는 Windows PowerShell 환경을 기준으로 합니다. 먼저 Anaconda 또는 Miniconda를
설치한 뒤 저장소 폴더에서 아래 명령을 한 번 실행합니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

스크립트는 다음 작업을 자동으로 수행합니다.

- Python 3.10 기반 `noplab` Conda 환경 생성 또는 업데이트
- `requirements.txt`의 라이브러리 설치
- Windows PowerShell 5와 PowerShell 7 프로필에 `noplab` 명령 등록
- 사용자 범위 PowerShell 실행 정책을 `RemoteSigned`로 설정

설정 후 현재 터미널을 닫고 새 PowerShell 터미널을 엽니다. 어느 폴더에서든 다음처럼
환경을 켤 수 있습니다.

```powershell
noplab
python --version
```

프롬프트 앞에 `(noplab)`이 표시되고 Python 버전이 `3.10.x`이면 정상입니다. 환경을
종료할 때는 환경 이름 없이 `conda deactivate`를 실행합니다.

```powershell
conda deactivate
```

프로필 단축 명령 없이 표준 Conda 명령을 사용해도 됩니다.

```powershell
conda activate noplab
```

## 실험 실행 순서

1. 장비 없이 짧은 더미 실험 실행:

   ```powershell
   python experiment.py --output data\dummy.csv
   ```

2. 저장된 결과를 PNG로 저장하고 화면에도 표시:

   ```powershell
   python plot_data.py data\dummy.csv --save data\dummy_plot.png
   ```

3. 실제 Keithley 2450으로 실행:

   ```powershell
   python experiment.py --real --resource "USB0::0x05E6::0x2450::INSTR" --current-limit 0.1
   ```

연결 주소는 NI MAX 또는 PyVISA의 resource 목록에서 확인해 바꿔야 합니다. USB 외에
GPIB/TCPIP 주소도 PyVISA resource 문자열로 지정할 수 있습니다. 기본 sweep은 0~2 V,
0.5 V 간격이며 `--start`, `--stop`, `--step`, `--dwell`, `--interval`, `--length`로
변경할 수 있습니다. `python experiment.py --help`에서 모든 옵션을 확인할 수 있습니다.

## GitHub로 팀원에게 배포

가상환경 본체는 용량이 크고 PC마다 경로가 달라 GitHub에 올리지 않습니다. 이 저장소의
`environment.yml`, `requirements.txt`, `setup_windows.ps1`이 같은 환경을 재생성합니다.
팀원은 저장소를 clone 또는 ZIP으로 내려받은 뒤 위의 Windows 최초 설치 명령을 한 번만
실행하면 됩니다. `.gitignore`는 Python 캐시, 개인 IDE 설정, 생성된 실험 데이터를
업로드 대상에서 제외합니다.

## 안전 및 확장

실험 정상 종료, 예외, `Ctrl+C` 중단 모두 `finally` 안전 처리에서 출력 전압을 먼저
0 V로 설정하고 output을 OFF한 뒤 연결을 닫습니다. 단, PC/케이블 단절 같은 하드웨어
통신 불능 상황에는 소프트웨어 명령 전달을 보장할 수 없으므로 장비의 전류 제한과
물리적 비상 정지 수단을 함께 사용해야 합니다.

Keithley 2460 지원은 `SourceMeasureUnit`을 상속하는 별도 클래스로 SCPI 차이만 구현한
뒤 `ExperimentRunner`에 전달하면 됩니다. 실제 레이저 센서는
`LaserDisplacementSensor`의 `connect`, `read_displacement`, `close`를 해당 제조사의
프로토콜에 맞게 구현하면 실험 실행부를 바꾸지 않고 사용할 수 있습니다.
