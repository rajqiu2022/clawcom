"""
通用图片上传 API
图片存储到 static/uploads/，通过 URL 引用
"""
import os
import uuid
from datetime import datetime
from flask import request, jsonify, current_app
from app.api import api_bp

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@api_bp.route('/upload', methods=['POST'])
def upload_file():
    """上传图片文件

    支持两种方式：
    1. multipart/form-data: file 字段
    2. 可选 source 参数标记来源（knowledge/testcase/general）

    返回：
    {
        "url": "/static/uploads/20260409/abc123.png",
        "filename": "abc123.png",
        "size": 12345
    }
    """
    if 'file' not in request.files:
        return jsonify({'error': '未找到文件，请使用 file 字段上传'}), 400

    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({'error': '未选择文件'}), 400

    if not _allowed_file(file.filename):
        return jsonify({'error': f'不支持的文件类型，允许: {", ".join(ALLOWED_EXTENSIONS)}'}), 400

    # 读取并检查大小
    file_data = file.read()
    if len(file_data) > MAX_FILE_SIZE:
        return jsonify({'error': f'文件过大，最大 {MAX_FILE_SIZE // 1024 // 1024}MB'}), 400

    # 按日期分目录
    date_dir = datetime.now().strftime('%Y%m%d')
    ext = file.filename.rsplit('.', 1)[1].lower()
    new_filename = f"{uuid.uuid4().hex[:12]}.{ext}"

    # 确定存储路径
    upload_dir = os.path.join(current_app.static_folder, 'uploads', date_dir)
    os.makedirs(upload_dir, exist_ok=True)

    filepath = os.path.join(upload_dir, new_filename)
    with open(filepath, 'wb') as f:
        f.write(file_data)

    url = f"/static/uploads/{date_dir}/{new_filename}"

    return jsonify({
        'url': url,
        'filename': new_filename,
        'original_name': file.filename,
        'size': len(file_data),
    }), 201
