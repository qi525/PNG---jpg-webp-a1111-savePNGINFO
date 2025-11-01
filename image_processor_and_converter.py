# image_processor_and_converter.py

# -*- coding: utf-8 -*-
import os
import re
import sys
import warnings 
import pandas as pd
import concurrent.futures # 导入 concurrent.futures 模块，用于实现线程池/进程池
from PIL import Image, ImageFile, ExifTags
from datetime import datetime
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE # 导入用于清理非法字符的正则
from typing import List, Dict, Any 
from tqdm import tqdm 
from loguru import logger 

# TODO 还是debug测试能不能生成webp，测试应该算是比较成功。【已完成】
# TODO 添加生成文件的模式，目标文件夹同级生成一个"PNG转JPG"或者"PNG转WEBP"的兄弟文件夹，然后把整个目标文件夹的目录结构全部复制过去，只是把png文件转换成jpg或者webp文件，其他文件不动。

# --- 新增导入和常量 (用于正确的 EXIF 写入) ---
import piexif 
import piexif.helper # 新增导入 piexif.helper 简化 UserComment 写入
# EXIF UserComment 标签 ID (0x9286)
EXIF_USER_COMMENT_TAG = 37510 
# EXIF ImageDescription 标签 ID (0x010E)
EXIF_IMAGE_DESCRIPTION_TAG = 270 
# 标准 EXIF UNICODE 头部 (8 字节)，已由 piexif.helper 处理，此常量不再需要
# UNICODE_HEADER = b"UNICODE\x00" 
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
# 为了调试方便，将控制台输出级别临时设置为 DEBUG
logger.configure(handlers=[
    {"sink": sys.stdout, "level": "DEBUG"} # DEBUG 级别可以捕获所有详细流程信息
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
        logger.error(f"[标准+兼容混合优化方案] 生成 EXIF 字节失败: {e}")
        return None

def convert_and_write_metadata(
    png_path: str, 
    raw_metadata: str, 
    output_format: str, 
    output_dir_base: str
) -> str | None:
    """
    写入过程核心函数：将 PNG 转换为目标格式，并将元数据写入新文件。
    
    !!! 注意: 本次更新采用 [优化方案] generate_exif_bytes。
    """
    logger.info(f"--- 正在处理文件: {os.path.basename(png_path)} ---")
    
    # 1. 构建新的输出路径和文件夹
    folder = os.path.dirname(png_path)
    output_sub_dir = os.path.join(folder, output_dir_base) 
    os.makedirs(output_sub_dir, exist_ok=True)
    
    base_name = os.path.splitext(os.path.basename(png_path))[0]
    new_file_name = f"{base_name}.{output_format}"
    output_path = os.path.join(output_sub_dir, new_file_name)
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
                    # 捕获 EXIF 准备过程中的错误
                    logger.error(f"为 '{output_path}' 准备 EXIF 元数据失败: {e}", exc_info=True)
                    logger.warning("将尝试不带 EXIF 写入图像文件。")
            
            # 4. 保存图像
            if output_format == 'jpg':
                # **关键步骤：JPG 模式转换**
                # JPG 不支持 Alpha 通道 (RGBA)，必须转换为 RGB
                if img.mode == 'RGBA':
                    logger.info("PNG 是 RGBA 模式，转换为 RGB 并填充白色背景。")
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3]) # 粘贴并使用 Alpha 通道作为蒙版
                    img = background
                elif img.mode != 'RGB':
                     logger.info(f"图像模式为 {img.mode}，转换为 RGB。")
                     img = img.convert('RGB')
                     
                logger.debug(f"开始保存 JPG 文件，最终模式: {img.mode}")
                img.save(output_path, 'jpeg', quality=95, **save_kwargs)
                
            elif output_format == 'webp':
                # WebP 保存，不需要强制转换为 RGB，但会尝试写入 EXIF
                logger.debug("开始保存 WEBP 文件。")
                img.save(output_path, 'webp', quality=95, **save_kwargs)
            else:
                logger.error(f"不支持的输出格式: {output_format}")
                return None
            
            logger.info(f"文件成功写入: {output_path}")
            return output_path
            
    except Exception as e:
        # 捕获文件读取或最终保存过程中的错误
        logger.error(f"转换或保存文件 '{png_path}' 到 '{output_path}' 失败: {e}", exc_info=True)
        return None

def process_conversion_task(png_path: str, output_format: str, output_dir_base: str) -> Dict[str, Any]:
    """
    [多线程工作单元] 处理单个 PNG 文件的提取、转换、写入和校验。
    """
    # 1. 提取原始 PNG 元数据
    raw_png_info = extract_metadata_from_png(png_path)
    
    # 清理元数据，移除首尾空格和换行符，确保元数据写入时是干净的
    if raw_png_info: # 检查是否成功提取到元数据
        raw_png_info = raw_png_info.strip() # 使用 strip() 清理字符串首尾的空白字符
        
    # 2. 执行转换和写入元数据
    new_file_path = convert_and_write_metadata( # 调用核心转换函数
        png_path, 
        raw_png_info, 
        output_format, 
        output_dir_base
    )
    
    # 3. 结果收集逻辑
    if new_file_path: # 检查文件是否成功生成
        # 扫描新文件的元数据进行对比
        new_file_scan_result = process_single_image(new_file_path) # 再次扫描新生成的文件进行元数据提取和结构化
        
        new_file_info_string = ( # 获取新文件的元数据字符串
            new_file_scan_result.get("去掉换行符的生成信息", "") 
            if new_file_scan_result else "未扫描到信息"
        )
        
        # 简化原始信息进行对比
        raw_png_info_no_newlines = raw_png_info.replace('\n', ' ').replace('\r', ' ').strip() # 清理原始元数据字符串
        
        # 对比结果
        is_consistent = "否" # 默认标记为不一致
        # 校验逻辑：新文件的元数据是否与原始元数据字符串一致
        if raw_png_info_no_newlines and raw_png_info_no_newlines == new_file_info_string:
            is_consistent = "是" # 如果一致，标记为“是”
        
        # 记录成功结果
        return { # 返回成功任务的结果字典
            "原文件的绝对路径": png_path,
            "原文件的pnginfo信息": raw_png_info_no_newlines,
            f"生成的{output_format.upper()}文件的绝对路径": new_file_path,
            f"生成的{output_format.upper()}文件的pnginfo信息": new_file_info_string,
            "原文件和生成文件的pnginfo信息是否一致": is_consistent,
            "success": True # 标记任务成功
        }
    else:
        # 记录失败结果
        # 由于 convert_and_write_metadata 失败时会返回 None，此处进行失败记录
        return { # 返回失败任务的结果字典
            "原文件的绝对路径": png_path,
            "原文件的pnginfo信息": raw_png_info.replace('\n', ' ').replace('\r', ' ').strip(),
            f"生成的{output_format.upper()}文件的绝对路径": "转换失败",
            f"生成的{output_format.upper()}文件的pnginfo信息": "转换失败",
            "原文件和生成文件的pnginfo信息是否一致": "否 (转换失败)",
            "success": False # 标记任务失败
        }


def main_conversion_process(root_folder: str, choice: int):
    """
    主处理流程，包括扫描、转换、生成报告。使用多线程并发处理文件。
    """
    
    # 1. 预处理
    output_format = 'jpg' if choice == 1 else 'webp' # 根据用户选择确定输出格式
    output_dir_base = f"png转{output_format.upper()}" # 定义输出子目录名称
    report_file = f"png_conversion_report_{output_format}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx" # 定义报告文件名称
    
    png_files = get_png_files(root_folder) # 扫描文件夹，获取所有 PNG 文件路径
    total_files = len(png_files) # 任务总数
    
    if not total_files: # 如果没有找到文件
        logger.info(f"在 '{root_folder}' 中未找到任何 PNG 文件。") # 打印日志
        return # 退出函数
    
    logger.info(f"在 '{root_folder}' 中发现 {total_files} 个 PNG 文件。将转换为 {output_format.upper()}。") # 打印任务信息
    
    conversion_results = [] # 初始化结果列表
    futures_to_path = {} # 初始化字典，用于存储 Future 对象和对应的文件路径
    success_count = 0 # 初始化成功计数器
    failure_count = 0 # 初始化失败计数器
    
    logger.info("--- 开始多线程文件转换处理 ---") # 打印多线程启动日志
    
    # 2. 转换和记录 (使用多线程)
    # 使用 ThreadPoolExecutor 实现多线程并发，适合 I/O 密集型任务（文件读写）
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor: # 实例化线程池执行器，并设置最大工作线程数
        
        # 遍历所有 PNG 文件，并将任务提交给线程池
        for png_path in png_files: # 遍历待处理的 PNG 文件列表
            # 提交任务到线程池，执行 process_conversion_task 函数
            future = executor.submit(process_conversion_task, png_path, output_format, output_dir_base) # 提交 worker 函数到线程池，传递必要的参数
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
                if result.get('success', False): # 根据结果字典中的 'success' 键判断任务是否成功
                    success_count += 1 # 成功任务计数加一
                else:
                    failure_count += 1 # 失败任务计数加一
                
            except Exception as exc: # 捕获任务执行过程中发生的任何异常
                logger.error(f"文件 '{png_path}' 转换任务异常终止: {exc}") # 记录异常错误日志
                failure_count += 1 # 任务异常，失败任务计数加一
                # 添加一个失败记录到结果列表
                conversion_results.append({ # 添加失败任务的结果字典
                    "原文件的绝对路径": png_path,
                    "原文件的pnginfo信息": "任务异常",
                    f"生成的{output_format.upper()}文件的绝对路径": "转换失败 (任务异常)",
                    f"生成的{output_format.upper()}文件的pnginfo信息": "转换失败 (任务异常)",
                    "原文件和生成文件的pnginfo信息是否一致": "否 (任务异常)",
                    "success": False # 标记为失败
                })

    # 3. 结果总结和 Excel 报告生成
    logger.info("\n--- 转换总结 ---")
    logger.info(f"总数量: {total_files}, 成功: {success_count}, 失败: {failure_count}")

    if conversion_results:
        try:
            df = pd.DataFrame(conversion_results)
            df.to_excel(report_file, index=False, engine='openpyxl')
            report_abs_path = os.path.abspath(report_file)
            logger.info(f"报告已成功生成: {report_abs_path}")
            # 4. 自动运行打开 Excel 报告
            os.startfile(report_abs_path) 
        except Exception as e:
            logger.error(f"生成 Excel 报告失败: {e}", exc_info=True)


if __name__ == "__main__":
    
    logger.info("--- PNG 图片批量转换和元数据校验工具启动 ---")
    logger.info("注意: 控制台日志级别已设置为 DEBUG，将输出详细流程信息。")
    
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

    # 3. 执行主流程
    main_conversion_process(root_folder, choice)
    
    logger.info("--- 任务完成 ---")