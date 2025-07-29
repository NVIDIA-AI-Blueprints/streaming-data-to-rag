import subprocess
from IPython.display import clear_output
import time
import requests

def wait_for_service(url, name=None, timeout=120, interval=5):
    """Wait for a service to be ready"""
    if name is None:
        name = url
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                print(f"✅ Service '{name}' is ready")
                return True
        except requests.exceptions.RequestException:
            pass
        print(f"⏳ Waiting {interval} seconds for service '{name}'...")
        time.sleep(interval)

    print(f"❌ Service at '{name}' failed to start within {timeout} seconds")
    return False

def tail_bash_command(cmd, n=10, interval=0.5):
    """Tail a bash command and display the output in the notebook"""
    proc = subprocess.Popen(
        cmd.split(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True
    )
    if proc.stdout is None:
        print("Failed to capture stdout")
        return

    buffer = []
    try:
        last_update_time = time.time()
        time_since_last_update = 0
        while True:
            line = proc.stdout.readline()
            if line:
                buffer.append(line.rstrip('\n'))

            time_since_last_update += time.time() - last_update_time
            if time_since_last_update < interval:
                continue
            else:
                last_update_time = time.time()
                time_since_last_update = 0

            if len(buffer):
                buffer = buffer[-n:]
                clear_output(wait=True)
                print("\n".join(buffer))

            if line == '' and proc.poll() is not None:
                break

    except KeyboardInterrupt:
        proc.terminate()
        print("Stopped.")

    print("✅ Done")

def docker_ps(ps_format="table {{.ID}}\t{{.Names}}\t{{.Status}}"):
    """Display running Docker containers with given formatting"""
    result = subprocess.run(
        ["docker", "ps", "--format", ps_format],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    print(result.stdout)