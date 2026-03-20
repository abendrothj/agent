"""
Activity Monitor for Win11 Muscle Service
Detects user activity (GPU, keyboard, mouse) to avoid resource contention.

When user is active: requests are queued
When idle for 5+ min: queued requests are processed
"""

import asyncio
import time
import subprocess
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from collections import deque
from loguru import logger

# Windows-specific imports
try:
    import ctypes
    import win32api
    import win32con
except ImportError:
    # Fallback if pywin32 not installed
    ctypes = None
    win32api = None


class IdleStatus(Enum):
    """Current idle/activity status."""
    ACTIVE = "active"           # User is actively using machine
    TRANSITIONING = "transitioning"  # Recently became idle
    IDLE = "idle"               # Idle for threshold duration


@dataclass
class ActivitySnapshot:
    """Snapshot of system activity at a point in time."""
    timestamp: float
    gpu_percent: float           # 0.0-100.0
    user_active: bool            # Keyboard/mouse detected
    idle_duration_sec: float     # How long since last activity
    idle_status: IdleStatus


class ActivityMonitor:
    """Monitor Win11 system activity to schedule Muscle inference."""
    
    def __init__(
        self,
        gpu_threshold_percent: float = 30.0,
        idle_threshold_sec: float = 300.0,  # 5 minutes
        check_interval_sec: float = 5.0,
    ):
        self.gpu_threshold = gpu_threshold_percent
        self.idle_threshold = idle_threshold_sec
        self.check_interval = check_interval_sec
        
        self.last_activity_time = time.time()
        self.current_status = IdleStatus.ACTIVE
        self.last_snapshot: Optional[ActivitySnapshot] = None
        
        # Queue for pending requests
        self.request_queue: deque = deque(maxlen=100)  # Max 100 queued
        
        logger.info(
            f"ActivityMonitor initialized: "
            f"gpu_threshold={gpu_threshold_percent}%, "
            f"idle_threshold={idle_threshold_sec}s"
        )
    
    async def get_gpu_utilization(self) -> float:
        """Query NVIDIA GPU utilization via nvidia-smi."""
        try:
            cmd = [
                "nvidia-smi",
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader"
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=2
            )
            
            if result.returncode != 0:
                logger.warning(f"nvidia-smi failed: {result.stderr}")
                return 0.0
            
            # Output: "42 %" (with space and percent sign)
            gpu_str = result.stdout.strip().replace("%", "").strip()
            return float(gpu_str)
        
        except subprocess.TimeoutExpired:
            logger.warning("nvidia-smi timeout")
            return 0.0
        except Exception as e:
            logger.error(f"GPU utilization query failed: {e}")
            return 0.0
    
    def get_last_input_time(self) -> float:
        """Get elapsed time since last keyboard/mouse input (Windows-specific)."""
        try:
            if not win32api:
                # Fallback: assume active if we can't check
                return 0.0
            
            # Get struct_size for LASTINPUTINFO
            lastInputInfo = ctypes.Structure()
            lastInputInfo._fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            lastInputInfo.cbSize = ctypes.sizeof(lastInputInfo)
            
            # Call GetLastInputInfo
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lastInputInfo))
            
            # Get current tick count
            millis_since_input = ctypes.windll.kernel32.GetTickCount() - lastInputInfo.dwTime
            
            # Handle wraparound (unlikely but possible every 49.7 days)
            if millis_since_input < 0:
                millis_since_input = 0
            
            return millis_since_input / 1000.0  # Convert to seconds
        
        except Exception as e:
            logger.warning(f"Failed to get last input time: {e}. Assuming active.")
            return 0.0
    
    async def check_activity(self) -> ActivitySnapshot:
        """Check current system activity and return snapshot."""
        current_time = time.time()
        
        # Check GPU
        gpu_util = await self.get_gpu_utilization()
        
        # Check input
        last_input_sec = self.get_last_input_time()
        user_active = last_input_sec < 60  # Recent activity in last 60s
        
        if user_active or gpu_util > self.gpu_threshold:
            # User is active - update last activity time
            self.last_activity_time = current_time
            idle_duration = 0.0
        else:
            # Idle - calculate idle duration
            idle_duration = current_time - self.last_activity_time
        
        # Determine idle status
        if user_active or gpu_util > self.gpu_threshold:
            idle_status = IdleStatus.ACTIVE
        elif idle_duration < self.idle_threshold:
            idle_status = IdleStatus.TRANSITIONING
        else:
            idle_status = IdleStatus.IDLE
        
        self.current_status = idle_status
        
        snapshot = ActivitySnapshot(
            timestamp=current_time,
            gpu_percent=gpu_util,
            user_active=user_active,
            idle_duration_sec=idle_duration,
            idle_status=idle_status
        )
        
        self.last_snapshot = snapshot
        return snapshot
    
    async def monitor_loop(self):
        """Continuous background monitoring (logs activity state)."""
        logger.info("Activity monitoring started")
        
        while True:
            try:
                snapshot = await self.check_activity()
                
                status_emoji = {
                    IdleStatus.ACTIVE: "🎮",
                    IdleStatus.TRANSITIONING: "⏳",
                    IdleStatus.IDLE: "💤"
                }.get(snapshot.idle_status, "?")
                
                logger.debug(
                    f"{status_emoji} Status: {snapshot.idle_status.value} | "
                    f"GPU: {snapshot.gpu_percent:.1f}% | "
                    f"Idle: {snapshot.idle_duration_sec:.0f}s | "
                    f"Queue: {len(self.request_queue)}"
                )
                
                await asyncio.sleep(self.check_interval)
            
            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)
                await asyncio.sleep(self.check_interval)
    
    def is_idle(self) -> bool:
        """Is the system currently idle and available for inference?"""
        return self.current_status == IdleStatus.IDLE
    
    def queue_request(self, request_id: str, request_data: dict) -> bool:
        """Queue a request if not idle. Returns True if queued, False if accepted immediately."""
        if self.is_idle():
            return False  # Accepted immediately
        
        if len(self.request_queue) < self.request_queue.maxlen:
            self.request_queue.append({
                "request_id": request_id,
                "data": request_data,
                "enqueued_at": time.time()
            })
            logger.info(f"Queued request {request_id}. Queue depth: {len(self.request_queue)}")
            return True
        else:
            logger.warning(f"Request queue full ({self.request_queue.maxlen}). Rejecting request {request_id}")
            return None  # Queue full
    
    def get_queued_request(self) -> Optional[dict]:
        """Dequeue next request (when idle and available)."""
        if self.request_queue and self.is_idle():
            req = self.request_queue.popleft()
            wait_time = time.time() - req["enqueued_at"]
            logger.info(f"Processing queued request {req['request_id']} (waited {wait_time:.1f}s)")
            return req
        return None
    
    def get_status(self) -> dict:
        """Get current monitoring status (for diagnostics)."""
        if not self.last_snapshot:
            return {
                "status": "not_ready",
                "queue_depth": len(self.request_queue)
            }
        
        snap = self.last_snapshot
        return {
            "idle_status": snap.idle_status.value,
            "gpu_utilization_percent": snap.gpu_percent,
            "user_active": snap.user_active,
            "idle_duration_seconds": snap.idle_duration_sec,
            "idle_threshold_seconds": self.idle_threshold,
            "queue_depth": len(self.request_queue),
            "queue_capacity": self.request_queue.maxlen,
            "accepting_requests": self.is_idle(),
            "timestamp": snap.timestamp,
        }
