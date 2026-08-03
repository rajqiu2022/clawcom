# Hermes Agent 图片接收修复指南

根据KM文章657835，修复大赫(claw-14)和小赫(claw-10)无法接收图片的问题。

## 问题根因

| # | Bug | 现象 | 根因 |
|---|------|------|------|
| 1 | `_cache_media` 异常被 `logger.debug` 吞掉 | 图片消息静默丢失 | 下载/解密失败的 except 分支用了 `logger.debug` 而非 `logger.info` |
| 2 | `base64.b64decode(aes_key)` 报 Incorrect padding | 图片下载成功但AES解密失败 | 企微API返回的aes_key长度为43（不是4的倍数） |

## 需要修改的文件

只需修改1个文件：`gateway/platforms/wecom.py`

## 修复步骤

### 步骤1：SSH到服务器

**小赫 (claw-10)**：
```bash
ssh -p 36000 root@9.134.51.249
```

**大赫 (claw-14)**：
```bash
# 请确认大赫的服务器地址，可能是同一台或不同
ssh -p 36000 root@<大赫服务器地址>
```

### 步骤2：找到wecom.py文件

在服务器上执行：
```bash
# 查找wecom.py文件位置
find /opt/openclaw-agents -name 'wecom.py' -path '*/platforms/*' 2>/dev/null
# 或
find /root -name 'wecom.py' -path '*/platforms/*' 2>/dev/null
# 或
python3 -c "import hermes_agent; print(hermes_agent.__file__)"
```

通常位置：
- 小赫：`/opt/openclaw-agents/claw-10-xiaohe/.../platforms/wecom.py`
- 或pip安装路径：`/usr/lib/python3.x/site-packages/hermes_agent/gateway/platforms/wecom.py`

### 步骤3：备份原文件

```bash
cp /path/to/wecom.py /path/to/wecom.py.bak
```

### 步骤4：应用修复1 - 修改`_cache_media`方法

在`wecom.py`中找到`_cache_media`方法（约第717行），做以下修改：

**修改点1.1：在方法开头添加调试日志**

在`async def _cache_media(...):`之后添加：
```python
    # DEBUG: Log the media dict keys and values (excluding base64 data)
    media_debug = {k: (v[:50] if isinstance(v, str) and len(v) > 50 else v)
                   for k, v in media.items() if k != "base64"}
    has_base64 = "base64" in media and bool(media.get("base64"))
    has_url = bool(str(media.get("url") or "").strip())
    logger.info("[DEBUG_CACHE_MEDIA] kind=%s keys=%s has_base64=%s has_url=%s debug=%s",
                kind, sorted(media.keys()), has_base64, has_url, media_debug)
```

**修改点1.2：在`if not url:`分支添加日志**

```python
    url = str(media.get("url") or "").strip()
    if not url:
        logger.info("[DEBUG_CACHE_MEDIA] No URL found for kind=%s", kind)  # 新增这行
        return None
```

**修改点1.3：下载前添加日志**

在`try:`之前添加：
```python
    logger.info("[DEBUG_CACHE_MEDIA] Attempting download for kind=%s url=%s", kind, url[:120])
```

**修改点1.4：下载成功后添加日志**

在`raw, headers = await self._download_remote_bytes(url, max_bytes=ABSOLUTE_MAX_BYTES)`之后添加：
```python
    logger.info("[DEBUG_CACHE_MEDIA] Download succeeded for kind=%s size=%d content_type=%s",
                kind, len(raw), headers.get("content-type", "unknown"))
```

**修改点1.5：下载失败except块修改日志级别**

将：
```python
    except Exception as exc:
        logger.debug("[%s] Failed to download %s from %s: %s", self.name, kind, url, exc)
        return None
```
改为：
```python
    except Exception as exc:
        logger.info("[%s] Failed to download %s from %s: %s", self.name, kind, url, exc)
        return None
```

**修改点1.6：aes_key检查添加日志**

在`aes_key = str(media.get("aeskey") or "").strip()`之后添加：
```python
    logger.info("[DEBUG_CACHE_MEDIA] aes_key present=%s (len=%d)", bool(aes_key), len(aes_key))
```

**修改点1.7：解密成功添加日志**

在`raw = self._decrypt_file_bytes(raw, aes_key)`之后添加：
```python
        logger.info("[DEBUG_CACHE_MEDIA] Decryption succeeded for kind=%s decrypted_size=%d", kind, len(raw))
```

**修改点1.8：解密失败except块修改日志级别并添加traceback**

将：
```python
    except Exception as exc:
        logger.debug("[%s] Failed to decrypt %s from %s: %s", self.name, kind, url, exc)
        return None
```
改为：
```python
    except Exception as exc:
        import traceback
        logger.info("[%s] Failed to decrypt %s from %s: %s\nTraceback:\n%s", self.name, kind, url, exc, traceback.format_exc())
        return None
```

### 步骤5：应用修复2 - 修改`_decrypt_file_bytes`方法

在`wecom.py`中找到`_decrypt_file_bytes`方法（约第1018行），做以下修改：

**修改前**：
```python
    @staticmethod
    def _decrypt_file_bytes(encrypted_data: bytes, aes_key: str) -> bytes:
        if not encrypted_data:
            raise ValueError("encrypted_data is empty")
        if not aes_key:
            raise ValueError("aes_key is required")

        key = base64.b64decode(aes_key)
```

**修改后**：
```python
    @staticmethod
    def _decrypt_file_bytes(encrypted_data: bytes, aes_key: str) -> bytes:
        if not encrypted_data:
            raise ValueError("encrypted_data is empty")
        if not aes_key:
            raise ValueError("aes_key is required")

        # WeCom API may return aes_key without proper base64 padding;
        # pad with '=' to make the length a multiple of 4.
        padding_needed = (4 - len(aes_key) % 4) % 4
        padded_key = aes_key + "=" * padding_needed
        key = base64.b64decode(padded_key)
```

### 步骤6：重启Hermes Agent服务

```bash
# 重启小赫
systemctl restart hermes-gateway-claw-10.service
systemctl restart openclaw-sidecar-v2-claw-10.service

# 重启大赫（请替换为实际的claw ID）
systemctl restart hermes-gateway-claw-14.service
systemctl restart openclaw-sidecar-v2-claw-14.service
```

### 步骤7：验证修复

1. 通过企微给Bot发送图片
2. 检查Hermes Agent日志：`journalctl -u hermes-gateway-claw-10.service -f`
3. 确认图片被正确处理，没有"Failed to decrypt"错误

## 快速验证

修改后，发送图片时应看到类似日志：
```
INFO: [... ] [DEBUG_CACHE_MEDIA] Attempting download for kind=image url=...
INFO: [... ] [DEBUG_CACHE_MEDIA] Download succeeded for kind=image size=12345 content_type=image/jpeg
INFO: [... ] [DEBUG_CACHE_MEDIA] aes_key present=True (len=43)
INFO: [... ] [DEBUG_CACHE_MEDIA] Decryption succeeded for kind=image decrypted_size=12345
```

## 回滚方案

如果修复后出现问题，恢复备份：
```bash
cp /path/to/wecom.py.bak /path/to/wecom.py
systemctl restart hermes-gateway-claw-XX.service
```

---
**注意**：如果无法找到`wecom.py`文件位置，请提供服务器地址和SSH访问方式，我可以协助远程修复。
