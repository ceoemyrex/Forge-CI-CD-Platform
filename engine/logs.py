# engine/logs.py

import os
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class LogStreamer:
    """
    Handles real-time log streaming and persistence.
    - Writes logs to disk as they arrive
    - Streams logs over SSE without buffering
    """
    
    def __init__(self, storage_root: str):
        self.storage_root = storage_root
        self.log_handles = {}  # {run_id: file handle}
    
    def write(self, run_id: str, line: str):
        """
        Write a log line for a run.
        Timestamp and persist to disk.
        """
        log_dir = f"{self.storage_root}/logs"
        os.makedirs(log_dir, exist_ok=True)
        
        log_path = f"{log_dir}/{run_id}.log"
        
        # Timestamp the line
        ts = datetime.utcnow().isoformat()
        formatted_line = f"[{ts}] {line}\n"
        
        # Append to file
        try:
            with open(log_path, "a") as f:
                f.write(formatted_line)
        except Exception as e:
            logger.error(f"Error writing log for run {run_id}: {e}")
    
    def read(self, run_id: str, follow: bool = False):
        """
        Read logs for a run.
        If follow=True, yields new lines as they arrive.
        """
        log_path = f"{self.storage_root}/logs/{run_id}.log"
        
        if not os.path.exists(log_path):
            return
        
        # Yield existing content
        with open(log_path, "r") as f:
            for line in f:
                yield line.strip()
        
        # If follow, keep yielding new lines
        if follow:
            import time
            with open(log_path, "r") as f:
                f.seek(0, 2)  # Seek to end
                
                while True:
                    line = f.readline()
                    if line:
                        yield line.strip()
                    else:
                        time.sleep(0.1)
                        # Check if run is still running (todo)
                        break