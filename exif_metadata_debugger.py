# exif_metadata_debugger.py

# -*- coding: utf-8 -*-
import os
import sys
import re 
from loguru import logger
from PIL import Image
import piexif
from typing import Dict, Any, Tuple

# --- 配置和常量 ---

# 使用 loguru 配置日志，保证日志的完整性和可追踪性
LOG_FILE = f"exif_debugger_{os.path.basename(__file__).replace('.py', '')}.log"
logger.remove() # 移除默认配置
# 配置控制台输出
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
# 配置日志文件输出
logger.add(LOG_FILE, rotation="10 MB", level="DEBUG", encoding="utf-8")
logger.info("--- EXIF 元数据枚举调试工具启动 ---")

# 用户提供的文件路径 (请确保此路径在您的系统中是有效的)
TARGET_IMAGE_PATH = r"C:\stable-diffusion-webui\outputs\txt2img-images\2025-11-01\00001-2629889630.jpg"

# EXIF 标签常量
EXIF_USER_COMMENT_TAG = 37510  # 0x9286
EXIF_IMAGE_DESCRIPTION_TAG = 270 # 0x010E
UNICODE_HEADER = b"UNICODE\x00"

# --- 核心辅助函数：SD 参数提取 ---

def extract_sd_params_from_user_comment(raw_bytes: bytes) -> Tuple[str, str]:
    """
    [SD 参数核心提取逻辑]
    专门针对 Stable Diffusion 写入 UserComment 的特殊格式进行提取。
    策略：去除 UNICODE 头部，用 UTF-16LE 解码，然后移除所有空字节字符 (\x00)。
    
    返回: (解码后的原始字符串, 清洗后的 SD 参数字符串)
    """
    # 1. 移除 UNICODE 头部
    if raw_bytes.startswith(UNICODE_HEADER):
        data_bytes = raw_bytes[len(UNICODE_HEADER):]
    else:
        # 如果没有头部，则使用完整字节，但SD JPG通常应有
        data_bytes = raw_bytes
        
    raw_decoded_text = ""
    cleaned_text = ""

    try:
        # 2. UTF-16LE 解码 (这是 SD WebUI 写入 JPG EXIF 的标准方式)
        raw_decoded_text = data_bytes.decode('utf-16le', errors='replace')
        
        # 3. 移除空字符 (\x00) 进行清洗 (关键步骤，解决乱码/截断问题)
        cleaned_text = raw_decoded_text.replace('\x00', '').strip()
        
    except Exception as e:
        logger.error(f"SD 参数提取失败: {e}")
        
    return raw_decoded_text, cleaned_text

# --- 核心辅助函数：解码逻辑 ---

def decode_exif_bytes(tag_name: str, raw_bytes: bytes) -> Dict[str, str]:
    """
    通过枚举不同的编码方式来尝试解码原始 EXIF 字节。
    """
    logger.debug(f"[{tag_name}] 原始字节长度: {len(raw_bytes)} 字节")
    logger.debug(f"[{tag_name}] 原始字节 (前 50 字节): {raw_bytes[:50]!r}")
    
    decoding_results = {}
    
    # --- 1. EXIF 标准解码尝试 (针对 UserComment 或任何带有 UNICODE 头的) ---
    if raw_bytes.startswith(UNICODE_HEADER):
        # 使用新的提取函数获取标准解码结果（未清洗的）
        raw_decoded, _ = extract_sd_params_from_user_comment(raw_bytes)
        decoding_results['EXIF_STANDARD (UTF-16LE)'] = raw_decoded
        logger.info(f"[{tag_name}] 尝试 1 (标准): 成功解码为 UTF-16LE。")
    # 如果不是 UserComment，或者没有 UNICODE 头部，则使用完整字节进行 UTF-8 等尝试
    else:
        # 尝试标准 UTF-16LE 解码
        try:
            decoded_text = raw_bytes.decode('utf-16le', errors='replace')
            decoding_results['UTF-16LE (Generic)'] = decoded_text
            logger.info(f"[{tag_name}] 尝试 1 (通用): 成功解码为 UTF-16LE。")
        except:
             decoding_results['UTF-16LE (Generic)'] = "解码失败"
    
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


# --- 主分析函数 ---

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
        exif_dict = piexif.load(image_path)
        
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
            
            # 3. 打印详细结果并尝试识别 SD 参数 (基于字符串)
            for method, text in results.items():
                is_sd_params = "否"
                
                is_sd_params_match = None
                if isinstance(text, str):
                    # 仅对长度大于50的文本进行正则搜索，避免短小非SD参数的误报
                    if len(text) > 50: 
                        # 使用 ASCII 关键字进行匹配，此步骤通常失败，仅用于演示编码问题
                        is_sd_params_match = re.search(r'(prompt|Steps|Sampler|model|Negative)', text, re.IGNORECASE | re.DOTALL)
                
                if is_sd_params_match:
                    is_sd_params = "是 (!!!)"
                    if best_match is None:
                        best_match = method
                
                logger.info(f"  > 解码方式: {method:<25} | 是否包含 SD 参数: {is_sd_params}")
                
                if is_sd_params == "是 (!!!)":
                    logger.success(f"  >>> 成功提取信息 (匹配到关键词: '{is_sd_params_match.group(1)}') (前500字符):\n{text[:500]}...")

            # 4. **针对 UserComment 的最终提取和打印 (核心逻辑)**
            if tag_name == 'UserComment':
                
                # 4.1 原始字节打印 (用于调试空字符问题)
                raw_bytes_data = raw_data
                if raw_bytes_data.startswith(UNICODE_HEADER):
                    data_bytes = raw_bytes_data[len(UNICODE_HEADER):]
                    logger.info(f"UserComment 字节数据已移除 UNICODE 头部。剩余长度: {len(data_bytes)} 字节。")
                else:
                    data_bytes = raw_bytes_data
                    logger.warning("UserComment 字节数据未检测到 UNICODE 头部。")
                
                logger.critical(f"\n{'='*20} UserComment 原始字节数据 (REPR，前1024字节) {'='*20}")
                # 使用 repr() 打印字节串的原始表示，以便看到所有 \x00
                logger.critical(repr(data_bytes[:1024])) 
                logger.critical(f"{'='*20} UserComment 原始字节数据 (REPR，结束) {'='*20}\n")
                
                # 4.2 调用新的 SD 参数提取函数 (使用抽象后的核心逻辑)
                _, cleaned_text = extract_sd_params_from_user_comment(raw_data)
                
                if cleaned_text:
                    # 4.3 确认成功提取 (基于非空结果)
                    # 移除原先的正则匹配，避免因 Unicode 字符集不同导致的误判 WARNING
                    logger.critical(f"  > 解码方式: EXIF_CLEANED (UTF-16LE)    | 是否包含 SD 参数: 是 (!!!)")
                    logger.success(f"  >>> 成功提取信息 (基于 UTF-16LE 清洗策略，前500字符): \n{cleaned_text[:500]}...")
                    best_match = 'EXIF_CLEANED (UTF-16LE)'
                    
                    # 4.4 强制打印清洗后的完整内容，这是最终需要的 SD 参数！
                    logger.critical(f"\n{'='*20} UserComment 【SD 参数最终提取内容】 (请复制此部分) {'='*20}")
                    logger.critical(cleaned_text)
                    logger.critical(f"{'='*20} UserComment 【SD 参数最终提取内容】 (结束) {'='*20}\n")
                else:
                    logger.warning("SD 参数提取失败，清洗后内容为空。")
            
            # 5. 结论总结
            if best_match:
                # 修正：当 EXIF_CLEANED 成功时，强制输出 SUCCESS 结论。
                logger.critical(f"结论: {tag_name} 标签已成功定位 SD 参数，推荐使用 UTF-16LE 解码并去除空字符的策略进行最终提取。")
            else:
                logger.warning(f"结论: 标签 {tag_name} 未发现 SD 参数，请检查上方强制打印的原始字节内容。")

    except Exception as e:
        logger.error(f"处理文件时发生致命错误: {e}", exc_info=True)
        
    logger.info("--- 分析完成 ---")


if __name__ == "__main__":
    analyze_exif_metadata(TARGET_IMAGE_PATH)
    
    # 自动打开日志文件，方便检查结果
    log_abs_path = os.path.abspath(LOG_FILE)
    logger.info(f"详细日志已写入文件: {log_abs_path}")