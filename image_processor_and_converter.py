# image_processor_and_converter.py

# -*- coding: utf-8 -*-
import os
import re
import sys
import warnings 
import pandas as pd
import concurrent.futures # 导入 concurrent.futures 模块，用于实现线程池/进程池
import shutil # 新增：导入 shutil 用于文件复制（失败恢复机制）
from PIL import Image, ImageFile, ExifTags
from datetime import datetime
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE # 导入用于清理非法字符的正则
from typing import List, Dict, Any 
from tqdm import tqdm 
from loguru import logger 

# TODO 还是debug测试能不能生成webp，测试应该算是比较成功。【已完成】
# TODO 添加生成文件的模式，目标文件夹同级生成一个"PNG转JPG"或者"PNG转WEBP"的兄弟文件夹，然后把整个目标文件夹的目录结构全部复制过去，只是把png文件转换成jpg或者webp文件，其他文件不动。
# 【已完成，作为模式1】
# TODO 新增功能：完美移植原文件的创建时间和修改时间到转换后的文件，并支持验证。【已完成】

# --- 新增导入和常量 (用于正确的 EXIF 写入) ---
import piexif 
import piexif.helper # 新增导入 piexif.helper 简化 UserComment 写入
import file_timestamp_tools # 新增：导入时间戳工具模块
# EXIF UserComment 标签 ID (0x9286)
EXIF_USER_COMMENT_TAG = 37510 
# EXIF ImageDescription 标签 ID (0x010E)
EXIF_IMAGE_DESCRIPTION_TAG = 270 
# ---------------------------------------------


# 允许 Pillow 加载截断的图像文件，避免程序崩溃。
ImageFile.LOAD_TRUNCATED_IMAGES = True

# 全局变量，用于在警告处理函数中访问当前处理的文件路径
_current_processing_file = None

# 定义最大并发进程数 (通常是CPU核心数)
MAX_WORKERS = os.cpu_count() or 4

# 配置 Loguru (符合用户对日志的要求)
# 日志文件记录 ERROR 级别的信息
logger.add("image_processor_error.log", rotation="10 MB", level="ERROR", encoding="utf-8")
# 默认的控制台输出级别设置为 INFO
# **改动点：将控制台输出级别设置为 INFO，只输出重要信息和进度条，以精简控制台输出。**
logger.configure(handlers=[
    {"sink": sys.stdout, "level": "INFO"} # 级别调整为 INFO，只输出重要信息和进度条配合
])


# --- 正向提示词的停用词列表 (用于提取核心词) ---
POSITIVE_PROMPT_STOP_WORDS = [
    # ----------------------------------------------------
    # 核心词汇，一行算一个部分
    # (已根据用户要求，将每行视为一个整体词组)
    # ----------------------------------------------------
    # 第一行
    r"newest, 2025, toosaka_asagi, novel_illustration, torino_aqua, izumi_tsubasu, oyuwari, pottsness, yunsang, hito_komoru, akeyama_kitsune, fi-san, rourou_\(been\), gweda, fuzichoco, shanguier, anmi, missile228, ",
    "2025, toosaka_asagi, novel_illustration, torino_aqua, izumi_tsubasu, oyuwari, pottsness, ",
    "looking_at_viewer, curvy,seductive_smile,glamor,makeup,blush,, lace,ribbon,jewelry,necklace,drop earrings,pendant,, sexually suggestive,",
    # ----------------------------------------------------
    # 第二行
    "sexy and cute,",
    # ----------------------------------------------------
    # 第三行
    "dynamic pose, sexy pose,",
    # ----------------------------------------------------
    # 第四行 (包含质量标签和角度词)
    r"dynamic angle,, dutch_angle, tinker bell \(pixiv 10956015\),, masterpiece, best quality, amazing quality, very awa,absurdres,newest,very aesthetic,depth of field,",
    "very awa,absurdres,newest,very aesthetic,depth of field,",
]
# ------------------------------------------------------


def custom_warning_formatter(message, category, filename, lineno, file=None, line=None):
    """
    自定义警告格式化器，尝试获取当前处理的文件路径。
    """
    global _current_processing_file
    
    # 检查警告是否来自 PIL 的 TiffImagePlugin 并且是 Truncated File Read
    if category is UserWarning and "Truncated File Read" in str(message) and "TiffImagePlugin.py" in filename:
        if _current_processing_file:
            return f"UserWarning: {message} for file: '{_current_processing_file}'\n"
    
    # 对于其他警告，使用默认格式
    return warnings.formatwarning(message, category, filename, lineno, line)

# 设置自定义警告格式化器
warnings.formatwarning = custom_warning_formatter


def process_single_image(absolute_path: str) -> Dict[str, Any] | None:
    """
    处理单个图片文件，提取元数据并返回结构化数据。
    """
    global _current_processing_file 

    image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')
    
    if not os.path.exists(absolute_path) or not absolute_path.lower().endswith(image_extensions):
        return None 
    
    # 定义 Stable Diffusion 元数据信息的正则表达式模式
    sd_full_info_pattern = re.compile(
        r'.*?(?:masterpiece|score_\d|1girl|BREAK|Negative prompt:|Steps:).*?(?:Version:.*?|Module:.*?|)$',
        re.DOTALL # 允许.匹配换行符
    )
    # 定义一个更严格的正则，用于最终验证是否是有效的SD参数
    sd_validation_pattern = re.compile(r'Steps: \d+, Sampler: [\w\s]+', re.DOTALL)
    
    # 初始化变量
    containing_folder_absolute_path = os.path.abspath(os.path.dirname(absolute_path))
    sd_info = "没有扫描到生成信息"
    sd_info_no_newlines = "没有扫描到生成信息"
    positive_prompt = ""
    negative_prompt = ""
    other_settings = ""
    model_name = "未找到模型"
    positive_prompt_word_count = 0
    raw_metadata_string = ""
    creation_date_dir = "未获取日期"
    core_positive_prompt = "核心词为空" 

    _current_processing_file = absolute_path 

    try:
        # --- 获取文件创建日期 ---
        try:
            creation_time = os.path.getctime(absolute_path)
            dt_object = datetime.fromtimestamp(creation_time)
            creation_date_dir = dt_object.strftime("%Y-%m-%d")
        except Exception:
            pass 
        
        # --- 阶段 1: 开始图像元数据提取 ---
        with Image.open(absolute_path) as img:
            logger.debug(f"正在尝试提取文件: {absolute_path}, 格式: {img.format}")

            # 1.1 PNG 格式：从 'parameters' 字段提取
            if "png" in img.format.lower() and "parameters" in img.info:
                raw_metadata_string = img.info["parameters"]
                # 增强清理：移除首尾空白字符
                if raw_metadata_string:
                    raw_metadata_string = raw_metadata_string.strip()
                logger.debug("从 PNG 'parameters' 字段提取到元数据。")
            
            # 1.2 JPEG/WebP 格式：从 EXIF/ImageDescription 提取
            elif "jpeg" in img.format.lower() or "webp" in img.format.lower():
                if hasattr(img, '_getexif'):
                    exif_data = img._getexif()
                    if exif_data:
                        # 0x9286: UserComment, 0x010E: ImageDescription
                        # 遍历 UserComment 和 ImageDescription 标签
                        for tag, value in exif_data.items():
                            if tag in [EXIF_USER_COMMENT_TAG, EXIF_IMAGE_DESCRIPTION_TAG]: 
                                try:
                                    if isinstance(value, bytes):
                                        
                                        # *** 修复点: 优先尝试 EXIF 标准的 UTF-16LE 解码 (针对 UserComment) ***
                                        # 使用 piexif.helper.UserComment.load 尝试标准解码
                                        if tag == EXIF_USER_COMMENT_TAG:
                                            try:
                                                # piexif.helper.UserComment.load 会自动处理 UNICODE\x00 头部并解码 UTF-16LE
                                                decoded_value = piexif.helper.UserComment.load(value)
                                                raw_metadata_string = decoded_value
                                                # 增强清理：移除首尾空白字符
                                                if raw_metadata_string:
                                                    raw_metadata_string = raw_metadata_string.strip()
                                                logger.debug("从 EXIF UserComment 标签 (piexif.helper 标准解码) 提取到元数据。")
                                                break # 解码成功，跳出内部循环
                                            except Exception:
                                                # 如果不是标准格式，将继续尝试 Fallback
                                                pass 
                                        
                                        # Fallback: 兼容性解码 (兼容非标准的元数据，包括 ImageDescription 的 UTF-8/Latin-1)
                                        # 尝试 UTF-8 解码，如果失败尝试 latin-1
                                        decoded_value = value.decode('utf-8', errors='ignore')
                                        if not re.search(r'Steps:', decoded_value):
                                            decoded_value = value.decode('latin-1', errors='ignore')
                                        raw_metadata_string = decoded_value
                                        # 增强清理：移除首尾空白字符
                                        if raw_metadata_string:
                                            raw_metadata_string = raw_metadata_string.strip()
                                        logger.debug("从 EXIF 标签 (UTF-8/Latin-1 fallback) 提取到元数据。")

                                    elif isinstance(value, str):
                                        raw_metadata_string = value
                                    
                                    if raw_metadata_string and re.search(r'Steps:', raw_metadata_string):
                                        logger.debug(f"从 {img.format} EXIF 标签 {hex(tag)} 提取到元数据。")
                                        break
                                    elif raw_metadata_string:
                                        # 如果是 ImageDescription，可能不是完整 SD 字符串，但也要记录
                                        logger.debug(f"从 {img.format} EXIF 标签 {hex(tag)} 提取到非 SD 格式元数据。")

                                except Exception as e:
                                    logger.warning(f"EXIF 解码失败 for tag {hex(tag)}: {e}")
                                    pass
            
            # --- 阶段 2: 清理并使用更强大的正则表达式提取有效信息 ---
            if isinstance(raw_metadata_string, str) and raw_metadata_string:
                # 移除 Excel 不支持的非法 XML 字符
                cleaned_string = ILLEGAL_CHARACTERS_RE.sub(r'', raw_metadata_string)
                
                # 清理非标准头部，以防旧的非标准写入
                if cleaned_string.startswith("UNICODE"):
                    # 此时 raw_metadata_string 已经被 strip() 过，但为了保险，这里使用 lstrip() 清理内部头部
                    cleaned_string = cleaned_string[len("UNICODE"):].lstrip() 
                
                # 尝试使用 SD 信息块正则表达式捕获
                match = sd_full_info_pattern.search(cleaned_string)
                
                if match:
                    extracted_text = match.group(0).strip() 
                    # 再次使用更严格的正则验证
                    if sd_validation_pattern.search(extracted_text):
                        sd_info = extracted_text
                        sd_info_no_newlines = sd_info.replace('\n', ' ').replace('\r', ' ').strip()
                        logger.debug("SD信息块成功通过验证和切割。")
                        
                        # --- 阶段 3: 切割信息 ---
                        other_settings_match = re.search(r'(Steps:.*)', sd_info_no_newlines, re.DOTALL)
                        if other_settings_match:
                            other_settings = other_settings_match.group(1).strip()
                            temp_sd_info = sd_info_no_newlines[:other_settings_match.start()].strip()
                        else:
                            temp_sd_info = sd_info_no_newlines.strip()

                        negative_prompt_match = re.search(r'(Negative prompt:.*?)(?=\s*Steps:|$)', temp_sd_info, re.DOTALL)
                        if negative_prompt_match:
                            negative_prompt = negative_prompt_match.group(1).replace("Negative prompt:", "").strip()
                            positive_prompt = temp_sd_info[:negative_prompt_match.start()].strip()
                        else:
                            positive_prompt = temp_sd_info.strip()
                        
                        positive_prompt_word_count = len(positive_prompt)

                    else:
                        sd_info = "没有扫描到生成信息"
                        sd_info_no_newlines = "没有扫描到生成信息"
                        logger.debug("SD信息块未通过严格验证。")
                else:
                    sd_info = "没有扫描到生成信息"
                    sd_info_no_newlines = "没有扫描到生成信息"
                    logger.debug("未匹配到 SD 信息块的通用模式。")

            # --- 阶段 4: 提取正向提示词的核心词 ---
            core_positive_prompt = positive_prompt
            for word in POSITIVE_PROMPT_STOP_WORDS:
                core_positive_prompt = f" {core_positive_prompt} "
                core_positive_prompt = re.sub(re.escape(word), " ", core_positive_prompt, flags=re.IGNORECASE)
            
            core_positive_prompt = core_positive_prompt.strip()
            core_positive_prompt = re.sub(r'\s+', ' ', core_positive_prompt)
            if not core_positive_prompt:
                core_positive_prompt = "核心词为空"
                
            model_match = re.search(r'Model: ([^,]+)', other_settings)
            if model_match:
                model_name = model_match.group(1).strip()


    except Exception as e:
        # 捕获所有处理异常，并记录到日志文件
        logger.error(f"FATAL Error processing image file '{absolute_path}' : {e}", exc_info=True) 
    finally:
        _current_processing_file = None 

    return {
        "所在文件夹": containing_folder_absolute_path,
        "图片的绝对路径": absolute_path,
        "图片超链接": f'={absolute_path}',
        "stable diffusion的 ai图片的生成信息": sd_info,
        "去掉换行符的生成信息": sd_info_no_newlines, 
        "正面提示词": positive_prompt,
        "负面提示词": negative_prompt,
        "其他设置": other_settings,
        "正面提示词字数": positive_prompt_word_count, 
        "模型": model_name, 
        "创建日期目录": creation_date_dir, 
        "提取正向词的核心词": core_positive_prompt 
    }


def get_png_files(folder_path: str) -> List[str]:
    """
    扫描指定文件夹及其子文件夹，收集所有 PNG 文件的绝对路径。
    """
    png_files = []
    for root, dirs, files in os.walk(folder_path):
        if '.bf' in dirs:
            logger.warning(f"发现并跳过文件夹: {os.path.join(root, '.bf')}")
            dirs.remove('.bf')
            
        for file in files:
            if file.lower().endswith('.png'):
                png_files.append(os.path.abspath(os.path.join(root, file)))
    return png_files

def extract_metadata_from_png(file_path: str) -> str:
    """
    从 PNG 文件中提取原始 'parameters' 元数据字符串。
    """
    try:
        with Image.open(file_path) as img:
            if "png" in img.format.lower() and "parameters" in img.info:
                logger.debug(f"成功从 PNG 提取原始元数据: {file_path}")
                return img.info["parameters"]
            logger.debug(f"文件不是标准 PNG 或缺少 'parameters' 字段: {file_path}")
            return ""
    except Exception as e:
        logger.error(f"从 PNG 文件 '{file_path}' 提取元数据失败: {e}")
        return ""

# 新增辅助函数：计算目标输出子目录
def _get_output_sub_dir(
    input_path: str, 
    output_dir_base: str, 
    root_folder: str, 
    output_dir_type: int
) -> str | None:
    """
    根据输入文件路径和模式，计算目标输出子目录的绝对路径。
    """
    # 确保路径是绝对路径
    root_folder_abs = os.path.abspath(root_folder)
    input_path_abs = os.path.abspath(input_path)
    
    if output_dir_type == 1:
        # 模式 1: 目标文件夹同级，创建兄弟文件夹，并复刻目录结构
        # -----------------------------------------------------------
        parent_folder = os.path.dirname(root_folder)
        sibling_dir_path = os.path.join(parent_folder, output_dir_base)
        
        root_folder_name = os.path.basename(root_folder_abs)
        
        # 获取相对目录 (例如: 子文件夹A/子文件夹B)
        relative_dir = os.path.relpath(os.path.dirname(input_path_abs), root_folder_abs)
        
        # 构建新的输出子目录 (新逻辑: D:/PNG转JPG/转换目标/子文件夹A/子文件夹B)
        output_sub_dir = os.path.join(sibling_dir_path, root_folder_name, relative_dir)
        return output_sub_dir
        # -----------------------------------------------------------
        
    elif output_dir_type == 2:
        # 模式 2 (原有模式): 在当前文件所在的子文件夹内创建子目录
        # -----------------------------------------------------------
        folder = os.path.dirname(input_path)
        output_sub_dir = os.path.join(folder, output_dir_base) 
        return output_sub_dir
        # -----------------------------------------------------------
    else:
        return None


# 新增：用户保留的纯 UTF-8 兼容性写入方案
def get_exif_bytes_utf8_compatibility(raw_metadata: str) -> bytes | None:
    """
    [保留方案] 纯 UTF-8 双标签写入 EXIF。
    - UserComment: 写入纯 UTF-8 字节 (非标准，兼容部分外部软件)。
    - ImageDescription: 写入纯 UTF-8 字节 (兼容性最高的标签)。
    
    警告：UserComment 写入纯 UTF-8 非 EXIF 标准，可能无法被通用读取软件（如 Photoshop, Windows 属性）正确读取。
    """
    try:
        data_utf8 = raw_metadata.encode('utf-8', errors='ignore') 
        
        exif_dict = {
            # Exif IFD 存放 UserComment (非标准 UTF-8)
            "Exif": {
                EXIF_USER_COMMENT_TAG: data_utf8 
            },
            # 0th IFD 存放 ImageDescription (兼容性最高的 UTF-8 编码)
            "0th": {
                EXIF_IMAGE_DESCRIPTION_TAG: data_utf8
            }
        } 
        return piexif.dump(exif_dict)
    except Exception as e:
        logger.error(f"[UTF-8 兼容性方案] 生成 EXIF 字节失败: {e}")
        return None

# 重构：使用 piexif.helper.UserComment.dump 简化标准 UserComment 的生成
def generate_exif_bytes(raw_metadata: str) -> bytes | None:
    """
    [优化方案] EXIF 标准 UserComment (UTF-16LE, 使用 piexif.helper) + ImageDescription (UTF-8) 混合写入。
    - UserComment: 遵循 EXIF 标准 (UNICODE\x00 + UTF-16LE)。
    - ImageDescription: 写入纯 UTF-8 字节 (通用兼容)。
    """
    try:
        # 1. UserComment 标准编码：使用 piexif.helper.UserComment.dump 简化操作
        user_comment_bytes = piexif.helper.UserComment.dump(
            raw_metadata, 
            encoding="unicode" # 对应 EXIF 规范的 UTF-16LE 编码和 UNICODE\x00 头部
        )
        
        # 2. ImageDescription 兼容性编码 (UTF-8)
        # --- 保留的 UTF-8 兼容性/调试写法 (ImageDescription 标签) ---
        data_utf8 = raw_metadata.encode('utf-8', errors='ignore')
        
        # 3. 构造 piexif 字典
        exif_dict = {
            # Exif IFD 存放 UserComment (标准 UTF-16LE)
            "Exif": {
                EXIF_USER_COMMENT_TAG: user_comment_bytes 
            },
            # 0th IFD 存放 ImageDescription (兼容性 UTF-8 编码)
            "0th": {
                EXIF_IMAGE_DESCRIPTION_TAG: data_utf8
            }
        } 
        return piexif.dump(exif_dict)
    except Exception as e:
        # **改动：针对元数据过长导致的 piexif.dump 失败，记录更详细的警告**
        logger.error(f"[标准+兼容混合优化方案] 生成 EXIF 字节失败: {e}. **警告：这通常是由于元数据信息过长 (如 SD 提示词过长) 导致的写入失败**")
        return None

def convert_and_write_metadata(
    png_path: str, 
    raw_metadata: str, 
    output_format: str, 
    output_dir_base: str, # 保持不变，还是 "png转JPG" 或 "png转WEBP"
    root_folder: str, # 新增：原始根文件夹路径，用于模式1
    output_dir_type: int, # 新增：输出目录模式，1或2
    original_mtime_ts: float, # 新增：原始文件的修改时间戳
    original_ctime_ts: float  # 新增：原始文件的创建时间戳
) -> str | None:
    """
    写入过程核心函数：将 PNG 转换为目标格式，并将元数据写入新文件。
    
    !!! 安全提示: 本函数仅执行读取、转换和写入操作，不包含任何删除原文件的代码。
    """
    # 将文件处理状态信息降级到 DEBUG 级别
    logger.debug(f"--- 正在处理文件: {os.path.basename(png_path)} ---")
    
    # 1. 构建新的输出路径和文件夹
    output_sub_dir = _get_output_sub_dir(
        png_path, 
        output_dir_base, 
        root_folder, 
        output_dir_type
    )
    if not output_sub_dir:
        logger.error(f"无法获取输出目录，模式 {output_dir_type} 无效。")
        return None
        
    base_name = os.path.splitext(os.path.basename(png_path))[0]
    new_file_name = f"{base_name}.{output_format}"
    output_path = os.path.join(output_sub_dir, new_file_name)
    
    # 创建目标目录
    os.makedirs(output_sub_dir, exist_ok=True)
    logger.debug(f"目标输出路径: {output_path}")
    
    try:
        # 2. 读取图像
        with Image.open(png_path) as img:
            logger.debug(f"原始图像模式: {img.mode}")
            
            save_kwargs = {}
            if raw_metadata:
                logger.debug(f"原始元数据长度: {len(raw_metadata)}")
                
                # 3. 准备写入元数据到 EXIF
                try:
                    # **关键步骤：EXIF 写入 (调用优化方案)**
                    exif_bytes = generate_exif_bytes(raw_metadata)

                    if exif_bytes:
                        save_kwargs['exif'] = exif_bytes
                        logger.debug(f"EXIF 元数据准备完成 (优化方案: 标准 piexif.helper UserComment + UTF-8 ImageDescription)，字节大小: {len(exif_bytes)}")
                    # -------------------------------------------------------------------

                except Exception as e:
                    # 捕获 EXIF 准备过程中的错误（例如 piexif.dump 失败）
                    logger.error(f"为 '{output_path}' 准备 EXIF 元数据失败: {e}", exc_info=True)
                    logger.warning("将尝试不带 EXIF 写入图像文件。")
            
            # 4. 保存图像
            if output_format == 'jpg':
                # JPG 模式转换：RGBA -> RGB
                if img.mode == 'RGBA':
                    logger.debug("PNG 是 RGBA 模式，转换为 RGB 并填充白色背景。") 
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3]) # 粘贴并使用 Alpha 通道作为蒙版
                    img = background
                elif img.mode != 'RGB':
                    logger.debug(f"图像模式为 {img.mode}，转换为 RGB。")
                    img = img.convert('RGB')
                     
                logger.debug(f"开始保存 JPG 文件，最终模式: {img.mode}")
                img.save(output_path, 'jpeg', quality=95, **save_kwargs)
                
            elif output_format == 'webp':
                # WebP 保存
                logger.debug("开始保存 WEBP 文件。")
                img.save(output_path, 'webp', quality=95, **save_kwargs)
            else:
                logger.error(f"不支持的输出格式: {output_format}")
                return None
            
            logger.debug(f"文件成功写入: {output_path}")
            
            # --- 5. 写入原始时间戳 ---
            mtime_success = False
            ctime_success = False
            
            if original_mtime_ts > 0:
                # 优先设置 mtime (跨平台最可靠)
                mtime_success = file_timestamp_tools.modify_file_timestamps(output_path, original_mtime_ts)
                logger.debug(f"Mtime 写入结果: {'成功' if mtime_success else '失败'}")
                
            if original_ctime_ts > 0:
                # 尝试设置 ctime (Windows only)
                # 注意：modify_file_timestamps 会在 Windows 上尝试同时设置 mtime/atime/ctime
                # 尽管 mtime 已经设置过，但为了确保 ctime 被覆盖，可以再次调用。
                # 实际上，在 modify_file_timestamps 内部已处理，这里只需判断是否设置成功
                ctime_success = file_timestamp_tools.modify_file_timestamps(output_path, original_ctime_ts)
                # 重新检查 ctime 是否匹配（仅在 Windows 上有意义）
                current_ctime = os.stat(output_path).st_ctime
                # 检查 ctime 是否接近原始 ctime
                if abs(current_ctime - original_ctime_ts) < 1:
                    ctime_success = True
                else:
                    ctime_success = False
                logger.debug(f"Ctime 写入结果: {'成功' if ctime_success else '失败'}")

            # ---------------------------
            
            return output_path
            
    except Exception as e:
        # 捕获文件读取或最终保存过程中的错误
        # **改动：捕获最终保存失败的错误**
        logger.error(f"转换或保存文件 '{png_path}' 到 '{output_path}' 失败: {e}", exc_info=True)
        return None

def process_conversion_task(
    png_path: str, 
    raw_metadata: str, # 新增：预提取的原始元数据
    output_format: str, 
    output_dir_base: str, 
    root_folder: str, # 新增：根文件夹
    output_dir_type: int, # 新增：输出目录模式
    original_mtime_ts: float, # 新增：原始 mtime
    original_ctime_ts: float  # 新增：原始 ctime
) -> Dict[str, Any]:
    """
    [多线程工作单元] 处理单个 PNG 文件的提取、转换、写入和校验。
    """
    # 2. 执行转换和写入元数据
    new_file_path = convert_and_write_metadata( # 调用核心转换函数
        png_path, 
        raw_metadata, 
        output_format, 
        output_dir_base,
        root_folder, # 传递根文件夹
        output_dir_type, # 传递输出目录模式
        original_mtime_ts, # 传递原始 mtime
        original_ctime_ts  # 传递原始 ctime
    )
    
    # 3. 结果收集逻辑
    if new_file_path: # 检查文件是否成功生成
        # 成功逻辑
        
        # 扫描新文件的元数据进行对比
        new_file_scan_result = process_single_image(new_file_path) # 再次扫描新生成的文件进行元数据提取和结构化
        
        new_file_info_string = ( # 获取新文件的元数据字符串
            new_file_scan_result.get("去掉换行符的生成信息", "") 
            if new_file_scan_result else "未扫描到信息"
        )
        
        # 简化原始信息进行对比
        raw_png_info_no_newlines = raw_metadata.replace('\n', ' ').replace('\r', ' ').strip() # 清理原始元数据字符串
        
        # 对比结果
        is_consistent = "否" # 默认标记为不一致
        # 校验逻辑：新文件的元数据是否与原始元数据字符串一致
        if raw_png_info_no_newlines and raw_png_info_no_newlines == new_file_info_string:
            is_consistent = "是" # 如果一致，标记为“是”
            
        # --- 4. 时间戳验证逻辑 ---
        mtime_consistent = "否"
        ctime_consistent = "否"
        original_mtime_dt = datetime.fromtimestamp(original_mtime_ts).strftime("%Y-%m-%d %H:%M:%S")
        original_ctime_dt = datetime.fromtimestamp(original_ctime_ts).strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            stat_info_final = os.stat(new_file_path)
            final_mtime_ts = stat_info_final.st_mtime
            final_ctime_ts = stat_info_final.st_ctime
            final_mtime_dt = datetime.fromtimestamp(final_mtime_ts).strftime("%Y-%m-%d %H:%M:%S")
            final_ctime_dt = datetime.fromtimestamp(final_ctime_ts).strftime("%Y-%m-%d %H:%M:%S")

            # 校验 mtime (允许小于1秒的误差)
            if abs(final_mtime_ts - original_mtime_ts) < 1:
                mtime_consistent = "是"
            
            # 校验 ctime (Windows 上 ctime 即创建时间，允许小于1秒的误差)
            if abs(final_ctime_ts - original_ctime_ts) < 1:
                ctime_consistent = "是"

        except Exception as e:
            logger.error(f"校验新文件时间戳失败: {e}")
            final_mtime_dt = "校验失败"
            final_ctime_dt = "校验失败"
        # ---------------------------
        
        # 记录成功结果
        return { # 返回成功任务的结果字典
            "原文件的绝对路径": png_path,
            "原文件的pnginfo信息": raw_png_info_no_newlines,
            f"生成的{output_format.upper()}文件的绝对路径": new_file_path,
            f"生成的{output_format.upper()}文件的pnginfo信息": new_file_info_string,
            "原文件和生成文件的pnginfo信息是否一致": is_consistent,
            "原文件修改时间(mtime)": original_mtime_dt,
            "新文件修改时间(mtime)": final_mtime_dt,
            "Mtime移植是否成功": mtime_consistent,
            "原文件创建时间(ctime)": original_ctime_dt,
            "新文件创建时间(ctime)": final_ctime_dt,
            "Ctime移植是否成功(Win Only)": ctime_consistent,
            "任务执行状态": "成功", # 标记任务成功
            "是否需要触发全局警告": False # 成功不触发警告
        }
    else:
        # 失败逻辑：转换或保存失败 (包括元数据过长导致的保存失败)
        raw_png_info_no_newlines = raw_metadata.replace('\n', ' ').replace('\r', ' ').strip()
        
        # 失败处理：复制原始文件到目标目录
        output_sub_dir = _get_output_sub_dir(
            png_path, 
            output_dir_base, 
            root_folder, 
            output_dir_type
        )
        copied_path = "原始文件复制失败"
        if output_sub_dir:
            try:
                os.makedirs(output_sub_dir, exist_ok=True)
                # 复制原始 PNG 文件
                copied_filename = os.path.basename(png_path)
                copied_path_full = os.path.join(output_sub_dir, copied_filename)
                
                # 使用 copy2 复制文件，并保留元数据（如创建/修改时间）
                # 注意：copy2 会保留 mtime 和 ctime
                shutil.copy2(png_path, copied_path_full)
                copied_path = copied_path_full # 记录成功复制的路径
                logger.warning(f"由于转换失败，原始 PNG 文件已复制到: {copied_path}")
            except Exception as e:
                logger.error(f"复制失败文件 '{png_path}' 到 '{output_sub_dir}' 失败: {e}")
                copied_path = "原始文件复制失败 (文件系统错误)"
        
        # 提取原始时间，以便在失败报告中记录
        # ** FIX: 移除冗余且错误的 tasks_data 查找，直接使用函数参数 original_mtime_ts 和 original_ctime_ts **
        original_mtime_dt = datetime.fromtimestamp(original_mtime_ts).strftime("%Y-%m-%d %H:%M:%S")
        original_ctime_dt = datetime.fromtimestamp(original_ctime_ts).strftime("%Y-%m-%d %H:%M:%S")
        
        return { # 返回失败任务的结果字典
            "原文件的绝对路径": png_path,
            "原文件的pnginfo信息": raw_png_info_no_newlines,
            f"生成的{output_format.upper()}文件的绝对路径": f"转换失败，原始文件已复制到: {copied_path}",
            f"生成的{output_format.upper()}文件的pnginfo信息": "转换失败",
            "原文件和生成文件的pnginfo信息是否一致": "否 (转换失败)",
            "原文件修改时间(mtime)": original_mtime_dt,
            "新文件修改时间(mtime)": "转换失败",
            "Mtime移植是否成功": "否 (转换失败)",
            "原文件创建时间(ctime)": original_ctime_dt,
            "新文件创建时间(ctime)": "转换失败",
            "Ctime移植是否成功(Win Only)": "否 (转换失败)",
            "任务执行状态": "失败", # 标记为失败
            "是否需要触发全局警告": True # 失败需要触发一次警告
        }


def main_conversion_process(root_folder: str, choice: int, choice_dir: int):
    """
    主处理流程，包括扫描、转换、生成报告。使用多线程并发处理文件。
    
    参数:
    root_folder (str): 根文件夹路径。
    choice (int): 目标格式选择 (1: JPG, 2: WebP)。
    choice_dir (int): 输出目录模式选择 (1: 兄弟目录复刻, 2: 子文件夹旧模式)。
    """
    
    # 1. 预处理
    # 确保根文件夹是绝对路径，且末尾不带分隔符，方便 relpath 计算
    root_folder = os.path.abspath(root_folder) 
    
    output_format = 'jpg' if choice == 1 else 'webp' # 根据用户选择确定输出格式
    output_dir_base = f"PNG转{output_format.upper()}" # 定义输出子目录名称 (使用大写，与用户描述一致)
    report_file = f"png_conversion_report_{output_format}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx" # 定义报告文件名称
    
    png_files = get_png_files(root_folder) # 扫描文件夹，获取所有 PNG 文件路径
    total_files = len(png_files) # 任务总数
    
    if not total_files: # 如果没有找到文件
        logger.info(f"在 '{root_folder}' 中未找到任何 PNG 文件。") # 打印日志
        return # 退出函数
    
    logger.info(f"在 '{root_folder}' 中发现 {total_files} 个 PNG 文件。将转换为 {output_format.upper()}。") # 打印任务信息
    
    # 修复 Pylance 警告：由于此处只读取 MAX_WORKERS，无需使用 global 关键字。
    logger.info(f"本次任务将使用 {MAX_WORKERS} 个线程进行并发处理 (基于当前计算机的 CPU 核心数)。")

    # --- 任务准备：预提取元数据和时间戳 ---
    tasks_data = []
    for png_path in png_files:
        # 预提取元数据
        raw_metadata = extract_metadata_from_png(png_path)
        if raw_metadata:
            raw_metadata = raw_metadata.strip()
            
        # 预提取时间戳
        original_mtime_ts = 0.0
        original_ctime_ts = 0.0
        try:
            stat_info = os.stat(png_path)
            original_mtime_ts = stat_info.st_mtime
            original_ctime_ts = stat_info.st_ctime
        except Exception as e:
            logger.warning(f"获取文件时间戳失败 '{png_path}': {e}")
            
        tasks_data.append({
            "png_path": png_path,
            "raw_metadata": raw_metadata,
            "original_mtime_ts": original_mtime_ts, # 新增：原始 mtime
            "original_ctime_ts": original_ctime_ts  # 新增：原始 ctime
        })
    logger.info(f"已预提取 {len(tasks_data)} 个文件的原始元数据和时间戳。")
    # --------------------------------------------------------
    
    conversion_results = [] # 初始化结果列表
    futures_to_path = {} # 初始化字典，用于存储 Future 对象和对应的文件路径
    success_count = 0 # 初始化成功计数器
    failure_count = 0 # 初始化失败计数器
    
    logger.info("--- 开始多线程文件转换处理 ---") # 打印多线程启动日志
    
    # 2. 转换和记录 (使用多线程)
    # 使用 ThreadPoolExecutor 实现多线程并发，适合 I/O 密集型任务（文件读写）
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor: # 实例化线程池执行器，并设置最大工作线程数
        
        # 遍历所有任务数据，并将任务提交给线程池
        for task in tasks_data: # 遍历待处理的任务列表
            png_path = task['png_path']
            raw_metadata = task['raw_metadata']
            original_mtime_ts = task['original_mtime_ts'] # 从任务数据中获取 mtime
            original_ctime_ts = task['original_ctime_ts'] # 从任务数据中获取 ctime
            
            # 提交任务到线程池，执行 process_conversion_task 函数
            future = executor.submit(
                process_conversion_task, 
                png_path, 
                raw_metadata, # 传递预提取的元数据
                output_format, 
                output_dir_base,
                root_folder, # 传递根文件夹
                choice_dir, # 传递输出目录模式
                original_mtime_ts, # 传递原始 mtime
                original_ctime_ts  # 传递原始 ctime
            ) # 提交 worker 函数到线程池，传递必要的参数
            # 存储 Future 对象和对应的原始文件路径
            futures_to_path[future] = png_path # 将返回的 Future 对象作为键，文件路径作为值存入字典
        
        # 使用 concurrent.futures.as_completed 迭代已完成的 Future
        # 并结合 tqdm 来显示进度条
        progress_bar = tqdm( # 创建进度条
            concurrent.futures.as_completed(futures_to_path), # 迭代已完成的任务 Future
            total=total_files, # 设置进度条的总步数为文件总数
            desc=f"转换到 {output_format.upper()} 进度" # 进度条的描述文本
        )
        
        for future in progress_bar: # 遍历每一个已完成的 Future
            png_path = futures_to_path[future] # 从字典中获取该 Future 对应的文件路径
            try:
                result = future.result() # 获取线程执行的结果（即 process_conversion_task 的返回值）
                conversion_results.append(result) # 将结果字典添加到总列表中
                
                # 更新计数器
                if result.get('任务执行状态') in ["成功", "成功 (部分)"]: # 根据结果字典中的 '任务执行状态' 键判断任务是否成功
                    success_count += 1 # 成功任务计数加一
                else:
                    failure_count += 1 # 失败任务计数加一
                
            except Exception as exc: # 捕获任务执行过程中发生的任何异常
                logger.error(f"文件 '{png_path}' 转换任务异常终止: {exc}") # 记录异常错误日志
                failure_count += 1 # 任务异常，失败任务计数加一
                # 添加一个失败记录到结果列表
                
                # 任务异常失败逻辑
                # 获取原始时间戳（如果任务异常，这里只能依赖预提取的数据）
                task_data = next(task for task in tasks_data if task['png_path'] == png_path)
                original_mtime_ts = task_data.get('original_mtime_ts', 0.0)
                original_ctime_ts = task_data.get('original_ctime_ts', 0.0)
                original_mtime_dt = datetime.fromtimestamp(original_mtime_ts).strftime("%Y-%m-%d %H:%M:%S")
                original_ctime_dt = datetime.fromtimestamp(original_ctime_ts).strftime("%Y-%m-%d %H:%M:%S")
                
                conversion_results.append({ # 添加失败任务的结果字典
                    "原文件的绝对路径": png_path,
                    "原文件的pnginfo信息": "任务异常",
                    f"生成的{output_format.upper()}文件的绝对路径": "转换失败 (任务异常)",
                    f"生成的{output_format.upper()}文件的pnginfo信息": "转换失败 (任务异常)",
                    "原文件和生成文件的pnginfo信息是否一致": "否 (任务异常)",
                    "原文件修改时间(mtime)": original_mtime_dt,
                    "新文件修改时间(mtime)": "任务异常",
                    "Mtime移植是否成功": "否 (任务异常)",
                    "原文件创建时间(ctime)": original_ctime_dt,
                    "新文件创建时间(ctime)": "任务异常",
                    "Ctime移植是否成功(Win Only)": "否 (任务异常)",
                    "任务执行状态": "失败 (异常)", # 标记为失败
                    "是否需要触发全局警告": True # 任务异常需要触发警告
                })

    # 3. 结果总结和 Excel 报告生成
    logger.info("\n--- 转换总结 ---")
    logger.info(f"总数量: {total_files}, 成功: {success_count}, 失败: {failure_count}")

    # **新增: Everything 警告逻辑**
    needs_everything_warning = any(
        result.get("是否需要触发全局警告") # 检查是否有任务要求触发警告
        for result in conversion_results
    )
    if needs_everything_warning:
        logger.warning("-" * 50)
        logger.warning("【🔍 检查警报 🔍】")
        logger.warning("由于部分文件转换失败或信息写入不一致，建议使用 Everything 软件进行更多检查，以便快速定位未处理的原始 PNG 文件。")
        logger.warning("-" * 50)

    if conversion_results:
        try:
            df = pd.DataFrame(conversion_results)
            # 新增：元数据一致性校验统计
            inconsistent_count = (df['原文件和生成文件的pnginfo信息是否一致'].str.contains('否')).sum()
            inconsistent_mtime_count = (df['Mtime移植是否成功'] == '否').sum() # 新增：Mtime不一致校验
            logger.info(f"元数据不一致 (校验失败) 数量: {inconsistent_count} (请查看 Excel 报告中 '否 (转换失败)' 和 '否 (任务异常)' 的记录)")
            logger.info(f"Mtime 移植失败数量: {inconsistent_mtime_count} (请检查报告中的 'Mtime移植是否成功' 列)") # Mtime移植失败日志

            # 根据用户需求，日志和 Excel 报告都要自动运行打开
            report_abs_path = os.path.abspath(report_file)
            df.to_excel(report_file, index=False, engine='openpyxl')
            
            logger.info(f"报告已成功生成: {report_abs_path}")
            # 4. 自动运行打开 Excel 报告
            os.startfile(report_abs_path) 
        except Exception as e:
            logger.error(f"生成 Excel 报告失败: {e}", exc_info=True)


if __name__ == "__main__":
    
    # ** 核心安全警告：本工具仅执行读取和写入操作，不包含任何删除原始文件的功能。**
    logger.info("--- PNG 图片批量转换和元数据校验工具启动 ---")
    # 提示当前控制台级别已设置为 INFO
    logger.info("注意: 控制台日志级别已设置为 INFO，将只输出重要流程信息。详细 DEBUG/文件处理信息请通过修改代码查看。")
    
    # 1. 收集输入 - 文件夹路径
    while True:
        folder_path_input = input("请输入要扫描的文件夹绝对路径: ").strip()
        if os.path.isdir(folder_path_input):
            root_folder = folder_path_input
            break
        else:
            print("路径无效或文件夹不存在，请重新输入。")

    # 2. 收集输入 - 转换格式
    while True:
        try:
            choice_input = input("请选择转换格式 (1: JPG, 2: WebP): ").strip()
            choice = int(choice_input)
            if choice in [1, 2]:
                break
            else:
                print("无效的选择，请输入 1 或 2。")
        except ValueError:
            print("输入无效，请输入数字 1 或 2。")
            
    # 3. 收集输入 - 输出文件目录方式
    print("\n请选择输出文件目录方式：")
    print("  1. 目标文件夹同级，创建兄弟文件夹，**并将目标文件夹名称作为第一级子目录复刻完整结构** (例如: D:/Pictures/转换目标 -> D:/PNG转JPG/转换目标/...)")
    print("  2. 在每个子文件夹内创建对应的子目录 (例如: D:/Pictures/目标/子文件夹 -> D:/Pictures/目标/子文件夹/PNG转JPG/...)")
    while True:
        try:
            choice_dir_input = input("请选择输出目录模式 (1 或 2): ").strip()
            choice_dir = int(choice_dir_input)
            if choice_dir in [1, 2]:
                break
            else:
                print("无效的选择，请输入 1 或 2。")
        except ValueError:
            print("输入无效，请输入数字 1 或 2。")

    # **Windows Defender 性能警报 (仅 Windows)**
    if sys.platform.startswith('win'):
        # 确保使用全局 MAX_WORKERS
        logger.warning("-" * 50)
        logger.warning("【⚠️ 性能严重警告 ⚠️】")
        logger.warning(f"当前程序使用 {MAX_WORKERS} 线程进行高强度文件 I/O，可能导致 CPU 占用率接近 100%。")
        logger.warning("如果您运行在 Windows 系统，微软实时防护进程 MsMpEng.exe ('Antimalware Service Executable') 可能会扫描大量文件 I/O，极大地拖慢转换速度、抢占 CPU 资源，甚至造成系统卡死。")
        logger.warning("强烈建议您在运行本程序前：")
        logger.warning("  1. 暂时关闭 Windows Defender 实时保护。")
        logger.warning("  2. 将本程序的工作目录 (输入文件夹和输出文件夹) 添加到 Windows Defender 的排除项中。")
        logger.warning("-" * 50)
        
    # 4. 执行主流程
    main_conversion_process(root_folder, choice, choice_dir)
    
    logger.info("--- 任务完成 ---")
    
    # **程序结束暂停，等待用户回车关闭窗口**
    try:
        input("程序已执行完毕，请按回车键关闭窗口...")
    except EOFError:
        # 捕获在非交互式环境中运行时的 EOFError
        pass