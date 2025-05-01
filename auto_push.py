import subprocess
import time

def run_git_commands():
    try:
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-m", "auto upload longbench log"], check=True)
        subprocess.run(["git", "push"], check=True)
        print("Git push successful.")
    except subprocess.CalledProcessError as e:
        print(f"Error occurred: {e}")

if __name__ == "__main__":
    while True:
        run_git_commands()
        time.sleep(10)
