import os
import platform
import time
import ctypes
import re
from ctypes import wintypes
from datetime import datetime
from typing import Optional

# --- 核心功能 1：时间解析（读取） ---
def parse_time_from_filename(filename: str, time_format: str = "%Y%m%d_%H%M%S") -> Optional[float]:
    """
    尝试从文件名中提取时间字符串，并转换为Unix时间戳。

    Args:
        filename (str): 要解析的文件名。
        time_format (str): 文件名中时间字符串的格式，默认是 'YYYYMMDD_HHMMSS'。
                           函数会尝试查找一个与此格式长度匹配的数字串。

    Returns:
        Optional[float]: 提取到的Unix时间戳，如果解析失败则返回 None。
    """
    # 计算期望的时间字符串长度（例如 YYYYMMDD_HHMMSS 是 15 位）
    expected_len = len(time_format.replace('%', '').replace('_', '')) + time_format.count('_')
    
    # 构建一个匹配数字串的正则表达式
    # \d{8}_\d{6} 匹配 YYYYMMDD_HHMMSS
    pattern = r'(\d{8}_\d{6})' 
    
    match = re.search(pattern, filename)
    
    if match:
        time_string = match.group(1)
        try:
            dt_object = datetime.strptime(time_string, time_format)
            # print(f"文件名: {filename} -> 提取时间: {dt_object}") # 模块中不再使用 logger
            return dt_object.timestamp()
        except ValueError:
            # print(f"时间字符串 '{time_string}' 解析失败。")
            return None
    else:
        # print(f"文件名: {filename} -> 未找到匹配格式的时间串。")
        return None

# --- Windows FILETIME 转换（内部工具） ---
def _unix_time_to_filetime(unix_time: float) -> wintypes.FILETIME:
    """
    将Unix时间戳转换为Windows FILETIME结构体。
    """
    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]
    
    # 116444736000000000: 1601/1/1 到 1970/1/1 的 100 纳秒间隔数
    ft_val = int((unix_time * 10000000) + 116444736000000000)
    file_time = FILETIME()
    file_time.dwLowDateTime = ft_val & 0xFFFFFFFF
    file_time.dwHighDateTime = ft_val >> 32
    return file_time

# --- 核心功能 2：修改文件时间戳（写入） ---
def modify_file_timestamps(file_path: str, new_timestamp: float) -> bool:
    """
    修改单个文件的修改时间(mtime)、访问时间(atime)和创建时间(ctime)。

    Args:
        file_path (str): 文件的完整路径。
        new_timestamp (float): 目标Unix时间戳。

    Returns:
        bool: 时间戳修改是否成功。
    """
    if new_timestamp <= 0.0:
        # print("新的时间戳无效。")
        return False
        
    # 1. 修改访问时间(atime)和修改时间(mtime) (跨平台)
    try:
        os.utime(file_path, (new_timestamp, new_timestamp))
    except Exception:
        # print(f"修改 mtime/atime 失败。")
        return False
    
    # 2. 尝试修改创建时间(ctime) (仅限Windows)
    if platform.system() == "Windows":
        try:
            # 定义Windows API所需常量
            GENERIC_WRITE = 0x40000000
            OPEN_EXISTING = 3
            
            handle = ctypes.windll.kernel32.CreateFileW(
                file_path, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
            )

            if handle != -1:
                new_filetime = _unix_time_to_filetime(new_timestamp)
                
                # 调用 SetFileTime 函数，只设置创建时间
                ctypes.windll.kernel32.SetFileTime(
                    handle, ctypes.byref(new_filetime), None, None
                )
                
                ctypes.windll.kernel32.CloseHandle(handle)
        
        except Exception:
            # print(f"Windows API 修改创建时间过程中发生错误。")
            pass # 不影响主结果，允许失败

    # 3. 验证结果 (仅验证 mtime，因为它最可靠)
    try:
        stat_info = os.stat(file_path)
        # 允许小于1秒的误差
        if abs(stat_info.st_mtime - new_timestamp) < 1:
             return True
        else:
             # print("时间戳修改验证失败：修改后的mtime与目标时间不匹配。")
             return False
             
    except Exception:
        # print("验证文件时间戳时发生错误。")
        return False

# --- 示例用法 (可选，但有助于验证独立性) ---
if __name__ == "__main__":
    # 这是一个示例用法，实际项目中请导入并使用这些函数
    print("--- 模块测试：时间解析 ---")
    test_filename = "screenshot_20250101_123456.png"
    ts = parse_time_from_filename(test_filename)
    if ts:
        print(f"解析成功: {test_filename} -> {ts} ({datetime.fromtimestamp(ts)})")
    else:
        print(f"解析失败: {test_filename}")

    # 对于 modify_file_timestamps，需要一个真实的文件进行测试，这里只打印说明
    print("\n--- 模块测试：时间修改 ---")
    print("要测试 modify_file_timestamps，请在您的项目中提供一个真实文件路径和时间戳。")