# exif_metadata_debugger.py

# -*- coding: utf-8 -*-
import os
import sys
import re # <-- 修复点：导入正则表达式模块
from loguru import logger
from PIL import Image
import piexif
from typing import Dict, Any, Tuple

# --- 配置和常量 ---

# 使用 loguru 配置日志，保证日志的完整性和可追踪性
LOG_FILE = f"exif_debugger_{os.path.basename(__file__).replace('.py', '')}.log"
logger.remove() # 移除默认配置
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
logger.add(LOG_FILE, rotation="10 MB", level="DEBUG", encoding="utf-8")
logger.info("--- EXIF 元数据枚举调试工具启动 ---")

# 用户提供的文件路径 (请确保此路径在您的系统中是有效的)
TARGET_IMAGE_PATH = r"C:\stable-diffusion-webui\outputs\txt2img-images\2025-11-01\00001-2629889630.jpg"

# EXIF 标签常量
EXIF_USER_COMMENT_TAG = 37510  # 0x9286
EXIF_IMAGE_DESCRIPTION_TAG = 270 # 0x010E
UNICODE_HEADER = b"UNICODE\x00"

# --- 核心辅助函数 ---

def decode_exif_bytes(tag_name: str, raw_bytes: bytes) -> Dict[str, str]:
    """
    通过枚举不同的编码方式来尝试解码原始 EXIF 字节。
    """
    logger.debug(f"[{tag_name}] 原始字节 (前 50 字节): {raw_bytes[:50]!r}")
    
    decoding_results = {}
    
    # --- 1. EXIF 标准解码尝试 (针对 UserComment) ---
    if raw_bytes.startswith(UNICODE_HEADER):
        data_bytes = raw_bytes[len(UNICODE_HEADER):]
        try:
            decoded_text = data_bytes.decode('utf-16le', errors='replace')
            decoding_results['EXIF_STANDARD (UTF-16LE)'] = decoded_text
            logger.info(f"[{tag_name}] 尝试 1 (标准): 成功解码为 UTF-16LE。")
        except Exception as e:
            decoding_results['EXIF_STANDARD (UTF-16LE)'] = f"解码失败: {e}"
            logger.warning(f"[{tag_name}] 尝试 1 (标准): UTF-16LE 解码失败: {e}")
            
    # --- 2. 通用 UTF-8 解码尝试 ---
    try:
        decoded_text = raw_bytes.decode('utf-8', errors='replace')
        decoding_results['UTF-8'] = decoded_text
        logger.info(f"[{tag_name}] 尝试 2 (通用): 成功解码为 UTF-8。")
    except Exception as e:
        decoding_results['UTF-8'] = f"解码失败: {e}"
        logger.warning(f"[{tag_name}] 尝试 2 (通用): UTF-8 解码失败: {e}")
        
    # --- 3. 兼容 Latin-1 解码尝试 (单字节) ---
    try:
        decoded_text = raw_bytes.decode('latin-1', errors='replace')
        decoding_results['Latin-1'] = decoded_text
        logger.info(f"[{tag_name}] 尝试 3 (兼容): 成功解码为 Latin-1。")
    except Exception as e:
        decoding_results['Latin-1'] = f"解码失败: {e}"
        logger.warning(f"[{tag_name}] 尝试 3 (兼容): Latin-1 解码失败: {e}")

    # --- 4. 中文 GBK 解码尝试 ---
    try:
        decoded_text = raw_bytes.decode('gbk', errors='replace')
        decoding_results['GBK (Chinese)'] = decoded_text
        logger.info(f"[{tag_name}] 尝试 4 (中文): 成功解码为 GBK。")
    except Exception as e:
        decoding_results['GBK (Chinese)'] = f"解码失败: {e}"
        logger.warning(f"[{tag_name}] 尝试 4 (中文): GBK 解码失败: {e}")
        
    return decoding_results

def analyze_exif_metadata(image_path: str):
    """
    读取文件，提取 EXIF 元数据并进行枚举解码分析。
    """
    if not os.path.exists(image_path):
        logger.error(f"文件不存在: {image_path}")
        return

    logger.info(f"正在分析文件: {image_path}")
    
    try:
        # 1. 使用 piexif 加载 EXIF 数据
        # piexif 比 PIL 的 _getexif() 更可靠地提取所有 EXIF 块
        exif_dict = piexif.load(image_path)
        
        # 检查是否包含 EXIF 数据
        if not exif_dict:
            logger.error("文件中未找到 EXIF 元数据。")
            return

        tags_to_analyze = {
            "UserComment": (exif_dict.get("Exif", {}).get(EXIF_USER_COMMENT_TAG)),
            "ImageDescription": (exif_dict.get("0th", {}).get(EXIF_IMAGE_DESCRIPTION_TAG))
        }

        # 2. 遍历并分析每个重要标签
        for tag_name, raw_data in tags_to_analyze.items():
            logger.info(f"\n--- 开始分析标签: {tag_name} ---")
            
            if raw_data is None:
                logger.info(f"标签 {tag_name} (ID: {hex(EXIF_USER_COMMENT_TAG) if tag_name == 'UserComment' else hex(EXIF_IMAGE_DESCRIPTION_TAG)}) 未找到。")
                continue
                
            if not isinstance(raw_data, bytes):
                logger.warning(f"标签 {tag_name} 的数据不是字节类型 ({type(raw_data)})，跳过解码。")
                continue
            
            # 执行枚举解码
            results = decode_exif_bytes(tag_name, raw_data)
            
            logger.info(f"标签 {tag_name} 完整解码结果:")
            best_match = None
            
            # 3. 打印详细结果并尝试识别 SD 参数
            for method, text in results.items():
                is_sd_params = "否"
                # 使用 re.search 检查是否包含 SD 参数关键字
                if isinstance(text, str) and re.search(r'(Steps: \d+|Negative prompt:)', text): 
                    is_sd_params = "是 (!!!)"
                    if best_match is None:
                        best_match = method
                
                logger.info(f"  > 解码方式: {method:<25} | 是否包含 SD 参数: {is_sd_params}")
                
                # 为了不污染控制台，只打印 SD 参数匹配成功的文本
                if is_sd_params == "是 (!!!)":
                    logger.success(f"  >>> 成功提取信息:\n{text[:500]}...")
            
            # 4. 结论总结
            if best_match:
                logger.critical(f"结论: {tag_name} 标签极可能使用了 {best_match} 方式写入。")
            else:
                logger.warning(f"结论: 标签 {tag_name} 未发现 SD 参数或所有解码失败。")

    except Exception as e:
        logger.error(f"处理文件时发生致命错误: {e}", exc_info=True)
        
    logger.info("--- 分析完成 ---")


if __name__ == "__main__":
    analyze_exif_metadata(TARGET_IMAGE_PATH)
    
    # 自动打开日志文件，方便检查结果
    log_abs_path = os.path.abspath(LOG_FILE)
    logger.info(f"详细日志已写入文件: {log_abs_path}")
    try:
        os.startfile(log_abs_path)
    except Exception as e:
        logger.warning(f"无法自动打开日志文件: {e}")