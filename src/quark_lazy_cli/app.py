import html
import os
import sys
import json
import re
from datetime import datetime

from quark_lazy_cli.api import QasApi, QasApiError
from quark_lazy_cli.models import (
    DiskStatus,
    SearchGoal,
    SearchContext,
)
from quark_lazy_cli.advisor import (
    create_advisor,
    predict_latest_ep_from_urls,
    get_policy_desc,
    within_days,
    parse_ts,
    format_resource_line,
    normalize_taskname,
    simple_relative_day,
)
from quark_lazy_cli.config import settings
import logging
import logging.config
from logging.handlers import RotatingFileHandler

# ⚠️ 重要：此正则非常关键，不要轻易改动！AI Agent 禁止修改此配置
HARDCODE_LAZY_CLI_TV_REGEX_TEMPLATE = r".*?([Ss]\d{1,2})?(?:[第EePpXx\.\-\_\( ]{1,2}|^)(?<!\d){ep}(?![0-9KkPp]).*?\.(?:mp4|mkv)"

# ⚠️ 仅用于 _check_share_detail_with_specific_rules
# 只从 QAS preview 后的文件名中提取 EP，不参与正式候选匹配
# 不替代 HARDCODE_LAZY_CLI_TV_REGEX_TEMPLATE
QAS_PREVIEW_EP_EXTRACT_REGEX = re.compile(
    r".*?([Ss]\d{1,2})?(?:[第EePpXx\.\-\_\( ]{1,2}|^)(\d{1,3})(?!\d).*?\.(?:mp4|mkv)",
    re.IGNORECASE,
)

# ⚠️ 仅用于 _check_share_detail_with_specific_rules
# 匹配方括号内含数字的片段，如 [4K]、[2026.03.25]、[HEVC.AAC11]
NUMERIC_BRACKET_REGEX = re.compile(r"\[[^\]]*\d[^\]]*\]")


def scan_quark_userlocal_max_ep(file_list):
    """扫描目录文件，返回 (quark_userlocal_max_ep, eps)"""
    quark_userlocal_max_ep = 0
    eps = set()
    for f in file_list:
        if f.get("dir"):
            continue  # 跳过目录条目，只统计文件
        fname = f.get("file_name", "")
        match = re.search(r"(?i)(?:E|第)\s*0*(\d+)|^\s*(\d+)(?!\d)", fname)
        if match:
            ep_str = match.group(1) or match.group(2)
            if ep_str:
                ep = int(ep_str)
                if ep > 0:
                    eps.add(ep)
                    quark_userlocal_max_ep = max(quark_userlocal_max_ep, ep)
    return quark_userlocal_max_ep, eps


def _build_local_ep_pattern_from_replace(replace_tpl: str, taskname: str) -> re.Pattern | None:
    r"""
    把 QAS replace 模板转成严格 fullmatch 正则。
    - 普通文本全部 re.escape()
    - {TASKNAME} → re.escape(taskname)
    - {E}         → (?P<ep>\d+)
    - {EXT}       → (?P<ext>[A-Za-z0-9]+)（点由模板自身负责）
    - 不支持的占位符 → 返回 None（降级到旧逻辑）
    """
    if not replace_tpl:
        return None
    parts = []
    for part in re.split(r'(\{[^}]+\})', replace_tpl):
        if part == '{TASKNAME}':
            parts.append(re.escape(taskname))
        elif part == '{E}':
            parts.append(r'(?P<ep>\d+)')
        elif part == '{EXT}':
            parts.append(r'(?P<ext>[A-Za-z0-9]+)')
        elif part.startswith('{') and part.endswith('}'):
            return None
        else:
            parts.append(re.escape(part))
    return re.compile("^" + "".join(parts) + "$", re.IGNORECASE)


def _extract_local_ep_by_replace(fname: str, pattern: re.Pattern) -> int | None:
    """fullmatch 文件名，提取 EP"""
    m = pattern.fullmatch(fname)
    if m:
        ep_str = m.group("ep")
        if ep_str:
            return int(ep_str)
    return None


# ── Logging 配置（中心化，stdout/stderr 分流）──────────────────────────
LOG_DIR = os.path.expanduser(settings.log_dir)
os.makedirs(LOG_DIR, exist_ok=True)

# [mark] 投产环境不清空日志，保留历史记录
# if os.path.exists(f"{LOG_DIR}/cli.log"):
#     open(f"{LOG_DIR}/cli.log", "w").close()

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "file": {
            "format": "%(asctime)s [%(levelname)s] [%(name)s.%(funcName)s:%(lineno)d] %(message)s"
        },
        "console": {"format": "%(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "console",
            "stream": "ext://sys.stderr",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "DEBUG",
            "formatter": "file",
            "filename": f"{LOG_DIR}/cli.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
        },
    },
    "root": {"level": "DEBUG", "handlers": ["console", "file"]},
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)


class SanitizeFilter(logging.Filter):
    """过滤敏感信息：Cookie、Token、密码等不写入日志文件"""

    SENSITIVE_PATTERNS = [
        (r"(Cookie:\s*)[^\s]+", r"\1***"),
        (r"(pwd=)[^;&]+", r"\1***"),
        (r"(password=)[^;&]+", r"\1***"),
        (r"(token=)[^;&]+", r"\1***"),
        (r"(b-user-id=)[^;]+", r"\1***"),
        (r"(__sdid=)[^;]+", r"\1***"),
        (r"(Authorization:\s*Bearer\s+)[^\s]+", r"\1***"),
    ]

    def filter(self, record):
        msg = record.msg
        if isinstance(msg, str):
            for pattern, replacement in self.SENSITIVE_PATTERNS:
                msg = re.sub(pattern, replacement, msg, flags=re.IGNORECASE)
            record.msg = msg
        return True


for handler in logging.root.handlers:
    if isinstance(handler, RotatingFileHandler):
        handler.addFilter(SanitizeFilter())

if os.path.exists(".env"):
    with open(".env", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def output_agent_json(success, message="", data=None, code=0):
    res = {"success": success, "code": code, "message": message, "data": data or {}}
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    sys.exit(0 if success else 1)


def _format_size(size_bytes):
    if size_bytes >= 1073741824:
        return f"{size_bytes / 1073741824:.1f}GB"
    elif size_bytes >= 1048576:
        return f"{size_bytes / 1048576:.1f}MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes}B"


class QasLazyCli:
    def __init__(self, host=None, token=None):
        import logging

        self.logger = logging.getLogger(__name__)
        self.max_search_breadth = settings.max_search_breadth
        self.drill_max_depth = settings.drill_max_depth
        self.ep_filename_regex_template = HARDCODE_LAZY_CLI_TV_REGEX_TEMPLATE
        self.client = QasApi(host=host, token=token)
        # 缓存用于报告的状态
        #self._taskstatus_before_update = {"max": 0, "min": 0, "hole": []}
        #self._target_task_name_kword = ""  # 会在 _handle_update_command 入口处设置
        #self._target_task = None  # 当前 target_task

    # 对齐 QasAdvisor.normalize_taskname，保持接口一致
    _normalize_taskname = staticmethod(normalize_taskname)

    def _get_share_detail_with_qas_pattern(self, ctx, url, stoken=None):
        """临时用 $TV pattern 获取 preview，CLI EP 匹配逻辑不受影响"""
        if ctx.target_task is None:
            return self.client.get_share_detail(url, stoken=stoken)
        original_pattern = ctx.target_task.get("pattern", "")
        ctx.target_task["pattern"] = "$TV"
        detail = self.client.get_share_detail(url, stoken=stoken, task=ctx.target_task)
        ctx.target_task["pattern"] = original_pattern
        return detail

    @staticmethod
    def _parse_sse_result(text: str) -> dict:
        """解析 SSE 文本，提取 EP 编号"""
        result = {"success": "[DONE]" in text, "episodes": [], "raw": text[:500]}
        ep_regex = r".*?[第Ee]\s*0*(\d+).*?\.(?:mp4|mkv)"
        for line in text.split("\n"):
            if "🎞️" not in line:
                continue
            m = re.search(ep_regex, line, re.I)
            if m and m.group(1).isdigit():
                ep = int(m.group(1))
                if ep > 0:
                    result["episodes"].append(ep)
        return result

    def task_list(self):
        """列出所有订阅任务"""
        tasks = self.client.list_tasks()
        print(f"\n{'=' * 60}")
        print(f"任务列表 (共 {len(tasks)} 个)")
        print(f"{'=' * 60}\n")
        for t in tasks:
            print(f"  - {t.get('taskname', '')}")
            print(f"    保存路径: {t.get('savepath', '')}")
            print()

    def task_status(self, identifier):
        """显示任务状态"""
        data = self.client.get_data()
        tasklist = data.get("data", {}).get("tasklist", [])
        target_ids = [
            i
            for i, t in enumerate(tasklist)
            if (identifier.isdigit() and int(identifier) == i)
            or (identifier.lower() in t.get("taskname", "").lower())
        ]
        if not target_ids:
            print(f"未找到任务: {identifier}")
            return

        fresh_days = settings.search_expiration_days
        old_days = fresh_days * 2

        for tid in target_ids:
            task = tasklist[tid]
            taskname = task.get("taskname", "")
            savepath = task.get("savepath", "")

            # 纯净化剧名
            normalized_name = self._normalize_taskname(taskname)

            # 扫描本地（复用公共函数）
            scan_result = self._scan_task_local_status(task)
            if scan_result["access_failed"]:
                print(f"⚠️  {savepath} 访问失败或目录不存在，可能是个新订阅")
                return
            owned_eps = scan_result["owned_eps"]
            min_ep = scan_result["min_ep"]
            max_ep = scan_result["max_ep"]
            max_ep_mtime = scan_result["max_ep_mtime"]
            holes = scan_result["holes"]
            unmatch_files = scan_result["unmatch_files"]
            quark_userlocal_min_ep = min_ep
            quark_userlocal_max_ep = max_ep
            eps_count = scan_result["eps_count"]
            ep0_files = scan_result["ep0_files"]

            # 搜索资源（复用公共逻辑）
            try:
                unique_results, _, _, fresh_invalid_count = self._get_refined_search_results(normalized_name)
            except Exception as e:
                print(f"⚠️  搜索超时或网络错误：{e}")
                sys.exit(1)

            # 分类新鲜/旧资源（用于列表展示）
            fresh = [r for r in unique_results if within_days(parse_ts(r), fresh_days)]
            old = [
                r
                for r in unique_results
                if within_days(parse_ts(r), old_days)
                and not within_days(parse_ts(r), fresh_days)
            ]

            # 计算新鲜资源统计（与 advisor 格式一致）
            fresh_total = len(fresh) + fresh_invalid_count

            policy = settings.ep_selection_policy
            holes_str = (
                "无缺集"
                if not holes
                else f"缺 {holes}"
            )

            disk_info = f"最高第{quark_userlocal_max_ep}集（更新于：{simple_relative_day(max_ep_mtime)}）" if max_ep_mtime else f"最高第{quark_userlocal_max_ep}集"
            disk_info += f"，最低第{quark_userlocal_min_ep}集，{holes_str}"
            if scan_result["dup_eps"]:
                dup_detail = " ".join(f"[{ep}集 {eps_count[ep]}个文件]" for ep in sorted(scan_result["dup_eps"]))
                disk_info += f" ⚠️  发现重复剧集：{dup_detail}"
            if ep0_files:
                disk_info += f" ⚠️  发现 {len(ep0_files)} 个无效 EP0 文件：{', '.join(html.unescape(name) for name, _ in ep0_files[:10])}{'...' if len(ep0_files) > 10 else ''}"
            if unmatch_files:
                disk_info += f" ⚠️  发现 {len(unmatch_files)} 个文件不符合命名模板：{', '.join(html.unescape(f) for f in unmatch_files[:10])}{'...' if len(unmatch_files) > 10 else ''}"

            if fresh_invalid_count > 0:
                search_label = f"🔍 搜索到 {fresh_days} 天内新鲜资源：{fresh_total} 个，验证资源有效后：{len(fresh)} 个（其中 {fresh_invalid_count} 个失效）"
            else:
                search_label = f"🔍 搜索到 {fresh_days} 天内新鲜资源：{fresh_total} 个，验证资源有效后：{len(fresh)} 个"

            print(f"""
============================================================
  📋 订阅：{taskname}
  💾 当前：{disk_info}
  💎 偏好：{"最高画质优先" if policy == "prefer_quality" else "最大体积优先"}

============================================================
  [ {taskname} ] ⏳ Searching for {normalized_name}...

  {search_label}
============================================================
""")
            for i, r in enumerate(fresh):
                print(format_resource_line(i, r))
            print()
            for i, r in enumerate(old):
                print(format_resource_line(len(fresh) + i, r))

    def _scan_task_local_status(self, task: dict) -> dict:
        """
        扫描单个 task 的本地目录，返回扫描结果。
        task 需包含：taskname, savepath, replace
        """
        savepath = task.get("savepath", "")
        taskname = task.get("taskname", "")
        replace_tpl = task.get("replace", "")

        result = {
            "success": False,
            "access_failed": False,
            "owned_eps": set(),
            "eps_count": {},
            "min_ep": 9999,
            "max_ep": 0,
            "max_ep_mtime": 0,
            "holes": [],
            "dup_eps": [],
            "ep0_files": [],
            "unmatch_files": [],
            "all_files": [],
        }

        save_res = self.client.get_savepath_detail(path=savepath)
        returned_paths = save_res.get("data", {}).get("paths", [])
        if returned_paths:
            actual_dir = returned_paths[-1].get("name", "")
            target_dir = savepath.rstrip("/").split("/")[-1]
            if actual_dir != target_dir:
                result["access_failed"] = True
                return result

        filename_prefix = ""
        if replace_tpl:
            replaced = replace_tpl.replace("{TASKNAME}", taskname)
            filename_prefix = (
                replaced.rsplit(".S", 1)[0] + "."
                if ".S" in replaced
                else replaced.rsplit(".", 1)[0] + "."
            )

        ep_pattern = _build_local_ep_pattern_from_replace(replace_tpl, taskname)
        all_files = save_res.get("data", {}).get("list", [])

        for f in all_files:
            fname = f.get("file_name", "").strip()
            fsize = f.get("size", 0)
            if not fname or (filename_prefix and not fname.startswith(filename_prefix)):
                continue
            ep = None
            if ep_pattern:
                ep = _extract_local_ep_by_replace(fname, ep_pattern)
                if ep is None:
                    result["unmatch_files"].append(fname)
                    continue
            else:
                match = re.search(r"(?i)(?:E|第)\s*0*(\d+)", fname)
                if match:
                    ep = int(match.group(1))
                else:
                    result["unmatch_files"].append(fname)
                    continue
            if ep <= 0:
                result["ep0_files"].append((fname, fsize))
                continue
            result["owned_eps"].add(ep)
            result["eps_count"][ep] = result["eps_count"].get(ep, 0) + 1
            if ep > result["max_ep"]:
                result["max_ep"] = ep
                result["max_ep_mtime"] = f.get("updated_at", 0) or f.get("mtime", 0)
            elif ep == result["max_ep"]:
                mtime = f.get("updated_at", 0) or f.get("mtime", 0)
                if mtime > result["max_ep_mtime"]:
                    result["max_ep_mtime"] = mtime
            if ep < result["min_ep"]:
                result["min_ep"] = ep

        result["min_ep"] = result["min_ep"] if result["min_ep"] != 9999 else 1
        result["dup_eps"] = [ep for ep, cnt in result["eps_count"].items() if cnt > 1]
        result["holes"] = sorted(
            [
                e
                for e in range(result["min_ep"], result["max_ep"] + 1)
                if e not in result["owned_eps"] and e > 0
            ]
        )
        result["all_files"] = all_files
        result["success"] = True
        return result

    def _scan_quark_userlocal_directory(self, ctx) -> None:
        """扫描本地目录，结果写入 ctx 属性"""
        result = self._scan_task_local_status(ctx.task)
        if result["access_failed"]:
            print(f"⚠️  {ctx.task_config_profile['savepath']} 访问失败，可能是个新订阅")
            ctx.quark_userlocal_owned_eps_set = set()
            ctx.quark_userlocal_min_ep = 1
            ctx.quark_userlocal_max_ep = 0
            ctx.ep0_files = []
            ctx.dup_eps = []
            ctx.unmatch_files = []
            ctx.quark_userlocal_hole_eps_set = []
            ctx.all_files = []
            ctx.max_ep_mtime = 0
            ctx.snapshot_before = {
                "max": 0, "min": 1, "hole": [], "owned": set(), "last_updated": 0
            }
            return
        ctx.quark_userlocal_owned_eps_set = result["owned_eps"]
        ctx.quark_userlocal_min_ep = result["min_ep"]
        ctx.quark_userlocal_max_ep = result["max_ep"]
        ctx.max_ep_mtime = result["max_ep_mtime"]
        ctx.ep0_files = result["ep0_files"]
        ctx.dup_eps = result["dup_eps"]
        ctx.eps_count = result["eps_count"]
        ctx.unmatch_files = result["unmatch_files"]
        ctx.quark_userlocal_hole_eps_set = result["holes"]
        ctx.all_files = result["all_files"]
        ctx.snapshot_before = {
            "max": result["max_ep"],
            "min": result["min_ep"],
            "hole": result["holes"],
            "owned": result["owned_eps"],
            "last_updated": result["max_ep_mtime"],
        }

    def _get_refined_search_results(self, pure_name: str) -> tuple[list, int, int, int]:
        """
        公共搜索与过滤逻辑。
        返回 (valid_results, from_urls_predicted_latest_ep, invalid_share_count, fresh_invalid_count)
        """
        search_res = self.client.task_suggestions(pure_name, deep=True)
        if not search_res.get("success"):
            return [], 0, 0, 0

        unique_results, seen = [], set()
        for r in search_res.get("data", []):
            if r.get("shareurl") not in seen:
                seen.add(r.get("shareurl"))
                unique_results.append(r)

        # 标题模糊匹配过滤
        filtered = [
            r
            for r in unique_results
            if pure_name in (r.get("title") or r.get("taskname") or "")
        ]
        self.logger.debug(f"标题过滤: {len(unique_results)} -> {len(filtered)}")

        # 失效链接预验证
        valid_results, invalid_share_count = self.client.verify_share_links(filtered)
        self.logger.debug(f"链接验证: {len(filtered)} -> {len(valid_results)}")

        # 计算新鲜资源中过滤掉的失效数量
        fresh_days = settings.search_expiration_days
        fresh_before = sum(1 for r in filtered if within_days(parse_ts(r), fresh_days))
        fresh_after = sum(1 for r in valid_results if within_days(parse_ts(r), fresh_days))
        fresh_invalid_count = fresh_before - fresh_after

        # 推断最高 EP（基于有效结果）
        from_urls_predicted_latest_ep = 0
        for item in valid_results:
            t = item.get("title") or item.get("taskname") or ""
            ep = predict_latest_ep_from_urls(t)
            if ep and ep > from_urls_predicted_latest_ep:
                from_urls_predicted_latest_ep = ep

        return valid_results, from_urls_predicted_latest_ep, invalid_share_count, fresh_invalid_count

    @staticmethod
    def _extract_ep_from_rename_log(line: str, taskname: str, replace_tpl: str) -> int | None:
        """从 rename 日志行中提取 EP，用 rename 模板逆推匹配

        Args:
            line: SSE 日志行，如 "├── 🎞️深空彼岸.S01E01.mp4"
            taskname: 订阅名，如"深空彼岸"
            replace_tpl: rename 模板，如"{TASKNAME}.S01E{E}.{EXT}"

        Returns:
            EP 编号（如 1），解析失败返回 None
        """
        if "🎞️" not in line:
            return None
        escaped_name = re.escape(taskname)
        ep_pattern = replace_tpl.replace("{TASKNAME}", escaped_name).replace("{E}", r"(\d+)").replace("{EXT}", r"(?:mp4|mkv)")
        m = re.search(ep_pattern, line)
        if m:
            return int(m.group(1))
        return None

    def _resolve_task_meta(self, ctx) -> None:
        """将 task 解析为 task_config_profile，结果写入 ctx.task_config_profile"""
        t = ctx.task
        (
            savepath,
            taskname,
            original_pattern,
            original_replace,
            original_shareurl,
            original_pwd,
            original_startfid,
            original_enddate,
            original_runweek,
        ) = (
            t.get("savepath", ""),
            t.get("taskname", ""),
            t.get("pattern", r"'\$TV'"),
            t.get("replace", ""),
            t.get("shareurl", ""),
            t.get("pwd", ""),
            t.get("startfid", ""),
            t.get("enddate", ""),
            t.get("runweek", [1, 2, 3, 4, 5, 6, 7]),
        )
        filename_prefix = ""
        if original_replace:
            replaced = original_replace.replace("{TASKNAME}", taskname)
            filename_prefix = (
                replaced.rsplit(".S", 1)[0] + "."
                if ".S" in replaced
                else replaced.rsplit(".", 1)[0] + "."
            )
        ctx.task_config_profile = {
            "savepath": savepath,
            "taskname": taskname,
            "original_pattern": original_pattern,
            "original_replace": original_replace,
            "original_shareurl": original_shareurl,
            "original_pwd": original_pwd,
            "original_startfid": original_startfid,
            "original_enddate": original_enddate,
            "original_runweek": original_runweek,
            "original_shareurl_ban": t.get("shareurl_ban", ""),
            "filename_prefix": filename_prefix,
        }

    @staticmethod
    def _dedup_insert_original_shareurl(ctx) -> None:
        """去重并插入原始 shareurl 到列表开头"""
        if not ctx.task_config_profile.get("original_shareurl") or ctx.task_config_profile.get("original_shareurl_ban"):
            return
        orig_sid = (
            ctx.task_config_profile["original_shareurl"].split("/s/")[1].split("#")[0]
            if "/s/" in ctx.task_config_profile["original_shareurl"]
            else ""
        )
        dup_idx = None
        for i, u in enumerate(ctx.focused_urls_list):
            if "/s/" in u and u.split("/s/")[1].split("#")[0] == orig_sid:
                dup_idx = i
                break
        if dup_idx is not None:
            ctx.focused_urls_list.pop(dup_idx)
        ctx.focused_urls_list.insert(0, ctx.task_config_profile["original_shareurl"])

    def _parse_url_maxep(self, url: str) -> tuple[str, int]:
        """解析 URL，提取 maxep 参数，返回 (清理后URL, max_ep)

        支持格式：
        - ...?maxep=79
        - ...?pwd=122&maxep=79
        - ...?maxep=79&pwd=122

        返回清理后的 URL（移除 maxep 参数，保留其他如 pwd）
        """
        import re

        m = re.search(r'maxep=(\d+)', url)
        if not m:
            raise ValueError(
                f"URL 缺少 maxep 参数。正确格式：\n"
                f"  https://pan.quark.cn/s/xxx?pwd=122&maxep=79\n"
                f"  注意：用 & 分隔参数，不要用 ?"
            )
        max_ep = int(m.group(1))

        clean = re.sub(r'([?&])maxep=\d+', r'\1', url)
        clean = re.sub(r'[?&]+$', '', clean)
        clean = re.sub(r'\?&', '?', clean)
        clean = re.sub(r'&&', '&', clean)

        return clean, max_ep

    def _get_focused_urls_from_manual_url(self, ctx) -> None:
        """解析手动 URL，设置 ctx.focused_urls_list 和 ctx.advisor_confirmed_max_ep"""
        clean_url, max_ep = self._parse_url_maxep(ctx.input_url)
        if max_ep < ctx.quark_userlocal_max_ep:
            raise ValueError(
                f"maxep={max_ep} < 本地最高={ctx.quark_userlocal_max_ep}，无效"
            )
        if max_ep == ctx.quark_userlocal_max_ep and ctx.update_mode != "all":
            raise ValueError(
                f"maxep={max_ep} <= 本地最高={ctx.quark_userlocal_max_ep}，无新剧集（用 all 模式可补全缺失）"
            )
        self.logger.info(f"[URL解析] maxep={max_ep}, URL={clean_url}")
        ctx.focused_urls_list = [clean_url.strip()]
        ctx.advisor_confirmed_max_ep = max_ep

    def _get_focused_urls_via_search(self, ctx) -> None:
        """搜索资源并更新 ctx.focused_urls_list, ctx.advisor_confirmed_max_ep 等"""
        self.logger.info(
            f"  [ {ctx.task_config_profile['taskname']} ] ⏳ Searching for {ctx.target_task_name_kword}..."
        )
        unique_results, from_urls_predicted_latest_ep, _, fresh_invalid_count = (
            self._get_refined_search_results(ctx.target_task_name_kword)
        )
        if not unique_results:
            raise Exception("Automated search failed: No results found")

        advisor = create_advisor(debug=settings.debug)
        search_ctx = SearchContext(
            taskname=ctx.task_config_profile["taskname"],
            disk=DiskStatus(
                quark_userlocal_owned_eps_set=ctx.quark_userlocal_owned_eps_set,
                quark_userlocal_min_ep=ctx.quark_userlocal_min_ep,
                quark_userlocal_max_ep=ctx.quark_userlocal_max_ep,
                quark_userlocal_hole_eps_set=set(ctx.quark_userlocal_hole_eps_set),
                last_updated=ctx.max_ep_mtime,
            ),
            goal=SearchGoal(
                mode=ctx.update_mode,
                suggested_max_ep=from_urls_predicted_latest_ep,
            ),
            search_results=unique_results[: self.max_search_breadth],
            ep_selection_policy=ctx.ep_selection_policy,
            dup_eps=ctx.dup_eps,
            ep0_files=ctx.ep0_files,
            unmatch_files=ctx.unmatch_files,
            add_prompt=ctx.add_prompt,
            fresh_invalid_count=fresh_invalid_count,
        )
        decision = advisor.advice_search_result(search_ctx)
        ctx.advisor_kind = settings.advisor
        ctx.focused_urls_index = sorted(decision.advisor_selected_urls_index)
        ctx.advisor_max_ep = decision.advisor_confirmed_max_ep

        # LLM API 出错时直接报错告知用户
        if decision.error:
            raise Exception(f"ADVISOR_ERROR|{decision.error}")

        # 当无可选资源且确认最高EP不高于本地时，返回 early
        if not decision.advisor_selected_urls_index and (
            decision.advisor_confirmed_max_ep <= ctx.quark_userlocal_max_ep
        ):
            ctx.early_return = {
                "success": False,
                "no_update": True,
                "msg": "没有新剧集",
                "new_eps": [],
                "filled_eps": [],
                "advisor_kind": ctx.advisor_kind,
                "focused_urls_index": ctx.focused_urls_index,
                "advisor_max_ep": ctx.advisor_max_ep,
                "target_task": ctx.target_task,
                "target_task_name_kword": ctx.target_task_name_kword,
                "snapshot_before": ctx.snapshot_before,
            }
            return

        if (
            not decision.advisor_confirmed_max_ep
            and not decision.advisor_selected_urls_index
        ):
            ctx.early_return = {
                "success": False,
                "no_update": True,
                "msg": "无法确定最高剧集",
                "new_eps": [],
                "filled_eps": [],
                "advisor_kind": ctx.advisor_kind,
                "focused_urls_index": ctx.focused_urls_index,
                "advisor_max_ep": ctx.advisor_max_ep,
                "target_task": ctx.target_task,
                "target_task_name_kword": ctx.target_task_name_kword,
                "snapshot_before": ctx.snapshot_before,
            }
            return

        ctx.focused_urls_list = [
            unique_results[idx].get("shareurl")
            for idx in decision.advisor_selected_urls_index
        ]
        ctx.advisor_confirmed_max_ep = decision.advisor_confirmed_max_ep

    def _build_target_save_eps_and_selected_regex(self, ctx) -> str:
        # target_save_eps_set: 订阅目标集
        # 逻辑：基于本地扫描出的"空洞"(Holes) 和 顾问预测的"新剧集"(New) 构成的理想更新清单。
        # 代表了"本次更新任务的【应该】新增和补全哪些集"。
        quark_userlocal_hole_eps_set = {
            e
            for e in range(ctx.quark_userlocal_min_ep, ctx.quark_userlocal_max_ep + 1)
            if e not in ctx.quark_userlocal_owned_eps_set
        }
        ctx.target_save_eps_set = set(quark_userlocal_hole_eps_set)
        for ep in range(ctx.quark_userlocal_max_ep + 1, ctx.advisor_confirmed_max_ep + 1):
            ctx.target_save_eps_set.add(ep)
        if ctx.advisor_confirmed_max_ep <= ctx.quark_userlocal_max_ep:
            ctx.target_save_eps_set = {
                ep for ep in ctx.target_save_eps_set if ep <= ctx.advisor_confirmed_max_ep
            }
        if settings.debug:
            sys.stderr.write(
                f"\n[_build_target_save_eps_and_selected_regex] target_save_eps_set={ctx.target_save_eps_set}, quark_max_ep={ctx.quark_userlocal_max_ep}, advisor_confirmed_max_ep={ctx.advisor_confirmed_max_ep}\n"
            )
        if not ctx.target_save_eps_set:
            return ""
        ep_or = "|".join(f"0*{e}" for e in sorted(ctx.target_save_eps_set, reverse=True))
        target_regex = (
            f"(?:[第EePpXx\\.\\-\\_\\[\\(\\s]{{1,2}}|^)(?<!\\d)({ep_or})(?![\\dKkPp])"
        )
        self.logger.debug(f"动态合成扫描正则: {target_regex}")
        return target_regex

    @staticmethod
    def _empty_target_save_eps_return(ctx) -> dict:
        """当 target_save_eps_set 为空时返回 early exit dict"""
        if not ctx.focused_urls_list:
            return {
                "success": True,
                "no_update": True,
                "msg": "没有明确的目标剧集",
                "new_eps": [],
                "filled_eps": [],
                "advisor_kind": ctx.advisor_kind,
                "focused_urls_index": ctx.focused_urls_index,
                "advisor_max_ep": ctx.advisor_max_ep,
                "target_task": ctx.target_task,
                "target_task_name_kword": ctx.target_task_name_kword,
                "snapshot_before": ctx.snapshot_before,
            }
        return {
            "success": True,
            "no_update": True,
            "msg": "没有新剧集",
            "new_eps": [],
            "filled_eps": [],
            "advisor_kind": ctx.advisor_kind,
            "focused_urls_index": ctx.focused_urls_index,
            "advisor_max_ep": ctx.advisor_max_ep,
            "target_task": ctx.target_task,
            "target_task_name_kword": ctx.target_task_name_kword,
            "snapshot_before": ctx.snapshot_before,
        }

    def _no_candidate_return(self, ctx) -> dict:
        """当 bfs_result_pool 为空（候选资源失效）时返回带完整上下文的 dict"""
        original_ban = ctx.task_config_profile.get("original_shareurl_ban") or ""
        return {
            "success": False,
            "no_candidate": True,
            "msg": f"候选资源失效或未发现有效剧集：{original_ban}" if original_ban else "候选资源失效或未发现有效剧集",
            "new_eps": [],
            "filled_eps": [],
            "advisor_kind": ctx.advisor_kind,
            "focused_urls_index": ctx.focused_urls_index,
            "advisor_max_ep": ctx.advisor_max_ep,
            "target_task": ctx.target_task,
            "target_task_name_kword": ctx.target_task_name_kword,
            "target_save_eps_set": ctx.target_save_eps_set,
            "snapshot_before": ctx.snapshot_before,
        }

    def _parse_ep_from_preview_name(self, preview_name: str) -> int | None:
        m = QAS_PREVIEW_EP_EXTRACT_REGEX.search(preview_name or "")
        if not m:
            return None
        ep_val = m.group(2)
        if ep_val and ep_val.isdigit() and int(ep_val) > 0:
            return int(ep_val)
        return None

    def _check_share_detail_with_specific_rules(self, ctx, node_url: str, node_data: dict) -> dict:
        file_list = node_data["file_list"]
        has_numeric_bracket = False
        ep_to_raw_names = {}
        ep_to_preview_files = {}

        for f in file_list:
            if f.get("dir"):
                continue

            raw_name = f.get("file_name", "")
            if not re.search(r"\.(?:mp4|mkv)$", raw_name, re.I):
                continue

            if NUMERIC_BRACKET_REGEX.search(raw_name):
                has_numeric_bracket = True

            # file_name_re 和 file_name_saved 互斥
            preview_name = f.get("file_name_re") or f.get("file_name_saved") or ""
            if not preview_name:
                continue

            ep = self._parse_ep_from_preview_name(preview_name)
            if ep:
                ep_to_raw_names.setdefault(ep, set()).add(raw_name)
                ep_to_preview_files.setdefault(ep, []).append(f)

        dup_eps = {
            ep: sorted(raw_names)
            for ep, raw_names in ep_to_raw_names.items()
            if len(raw_names) > 1
        }

        dup_ep_preview_items = {
            ep: sorted(ep_to_preview_files.get(ep, []), key=lambda x: x.get("file_name", ""))
            for ep in dup_eps
        }

        signals = {
            "has_numeric_bracket_filename": has_numeric_bracket,
            "preview_duplicate_ep_count": len(dup_eps),
        }

        if has_numeric_bracket and len(dup_eps) >= 2:
            return {
                "ok": False,
                "rule_code": 1001,
                "rule_name": "NUMERIC_BRACKET_FILENAME_AND_PREVIEW_DUP_EP",
                "signals": signals,
                "details": {
                    "node_key": node_data.get("node_key", "?"),
                    "dir_name": node_data.get("dir_name", "?"),
                    "dup_eps": dup_eps,
                    "dup_ep_preview_items": dup_ep_preview_items,
                    "numeric_bracket_examples": [
                        f.get("file_name", "")
                        for f in file_list
                        if not f.get("dir") and NUMERIC_BRACKET_REGEX.search(f.get("file_name", ""))
                    ][:3],
                },
            }

        return {
            "ok": True,
            "rule_code": 0,
            "rule_name": "OK",
            "signals": signals,
            "details": {},
        }

    def _evaluate_files_for_target(
        self,
        ctx,
        file_list,
        link_vurl,
        *,
        show_debug: bool = False,
    ):
        active_regex = ctx.target_regex
        target_save_eps_set = ctx.target_save_eps_set
        advisor_confirmed_max_ep = ctx.advisor_confirmed_max_ep
        ep_selection_policy = ctx.ep_selection_policy
        if settings.debug and show_debug:
            sys.stderr.write(f"[_evaluate_files_for_target] [ENTER] file_list_len={len(file_list)}\n")
        if settings.debug and show_debug:
            sys.stderr.write(
                f"[_evaluate_files_for_target] target_save_eps_set={sorted(target_save_eps_set)}\n"
            )
            sys.stderr.write(f"[_evaluate_files_for_target] target_regex={active_regex[:80]}\n")
        eps_found, ep_to_info, ep_to_fid_mtime = set(), {}, {}
        ep_count = {}  # {ep: count} 同 EP 同 URL 匹配到的文件数（用于 download_method 决策）
        score = 0
        for f in file_list:
            raw_name = f.get("file_name", "")
            re_name = f.get("file_name_re", "")
            if not re_name:
                # file_name_re 为空：已在网盘存在或无法识别，跳过
                continue
            if not re.search(r"\.(?:mp4|mkv)$", raw_name, re.I):
                continue
            size, fid, mtime = (
                f.get("size", 0),
                f.get("fid", ""),
                f.get("l_updated_at", 0),
            )

            # 用干净的 re_name 做 EP 匹配：先转大写，去扩展名
            re_name_upper = re_name.upper()
            if not (re_name_upper.endswith(".MP4") or re_name_upper.endswith(".MKV")):
                # 非视频文件，跳过
                continue
            re_name_for_match = re_name_upper.rsplit(".", 1)[0]  # 去扩展名

            # 用去扩展名的干净名做 EP 匹配
            match = re.search(active_regex, re_name_for_match)
            if match:
                g_val = match.group(match.lastindex)
                if not (g_val and g_val.isdigit() and int(g_val) > 0):
                    continue
                ep_num = int(g_val)

                # 识别映射 debug
                if settings.debug:
                    sys.stderr.write(f"  [DEBUG] [app.py:{sys._getframe().f_lineno}] [RE-MATCH] 原始名: {raw_name[:40]} | RE名: {re_name_upper[:40]} | 去扩展名: {re_name_for_match[:40]} --> 命中 EP{ep_num}\n")

                # 过滤逻辑：必须在目标范围内，且不超过全局限制
                if ep_num not in target_save_eps_set or ep_num > advisor_confirmed_max_ep:
                    if settings.debug:
                        sys.stderr.write(f"  [DEBUG] [app.py:{sys._getframe().f_lineno}] ⏩ 跳过(不在目标范围): EP{ep_num}\n")
                    continue

                eps_found.add(ep_num)
                ep_count[ep_num] = ep_count.get(ep_num, 0) + 1
                should_rep = False
                reason = ""
                if ep_num not in ep_to_info:
                    should_rep = True
                    reason = "NEW_EP"
                else:
                    ext_size = ep_to_info[ep_num]["size"]
                    if ep_selection_policy == "prefer_size":
                        if size < ext_size:
                            should_rep, reason = True, "SMALLER_SIZE"
                        elif size == ext_size and mtime > ep_to_fid_mtime[ep_num]["mtime"]:
                            should_rep, reason = True, "NEWER_MTIME"
                    else:
                        if size > ext_size:
                            should_rep, reason = True, "LARGER_SIZE"
                        elif size == ext_size and mtime > ep_to_fid_mtime[ep_num]["mtime"]:
                            should_rep, reason = True, "NEWER_MTIME"
                if should_rep:
                    old_name = ep_to_info[ep_num]["filename"][:30] if ep_num in ep_to_info else "None"
                    if settings.debug:
                        sys.stderr.write(f"  [DEBUG] [app.py:{sys._getframe().f_lineno}] ✨ EP{ep_num} 入选: [{reason}] {old_name} --> {raw_name[:40]}\n")
                    ep_to_info[ep_num] = {
                        "filename": raw_name,  # 下载用原始文件名
                        "size": size,
                        "url": link_vurl,
                    }
                    ep_to_fid_mtime[ep_num] = {"fid": fid, "mtime": mtime}
                score += size // 1024
        if settings.debug and show_debug:
            sys.stderr.write(f"[_evaluate_files_for_target] [LEAVE] eps_found={sorted(eps_found)} ep_count={ep_count}\n")
        return eps_found, score, ep_to_info, ep_to_fid_mtime, ep_count


    @staticmethod
    def _calc_candidate_score(cand: dict, ep_selection_policy: str) -> int:
        if ep_selection_policy == "prefer_size":
            return -cand["size"]
        return cand["size"]

    def _recompute_planned_save_eps_after_pool(self, ctx) -> set:
        # attainable_to_save_eps_set: 可达成转存集
        # 逻辑：target_save_eps_set 与云端实际存在资源(Pool)的交集。
        # 代表了"目标清单中，目前【确实有货】且【可以执行转存】的集数"。
        # 关系：attainable_to_save_eps_set ⊆ target_save_eps_set
        all_pool_eps = set()
        for entry in ctx.bfs_result_pool:
            all_pool_eps |= entry["eps"]
        new_base = {e for e in all_pool_eps if e > ctx.quark_userlocal_max_ep}
        ctx.attainable_to_save_eps_set = set(new_base)
        if ctx.update_mode == "all":
            ctx.attainable_to_save_eps_set |= set(ctx.quark_userlocal_hole_eps_set)
        return ctx.attainable_to_save_eps_set

    def _format_bfs_file_preview_line(self, f: dict, marker: str = "•") -> str:
        name = f.get("file_name", "")
        fn_re = f.get("file_name_re", "")
        fn_saved = f.get("file_name_saved", "")
        fsize = f.get("size", 0)
        fmtime = f.get("l_updated_at", 0)
        if fmtime:
            import time

            mtime_str = time.strftime("%m-%d", time.localtime(fmtime / 1000))
        else:
            mtime_str = ""
        return f"  {marker} {name[:30]} | re={fn_re[:40]} | saved={fn_saved[:40]} | {_format_size(fsize)} | {mtime_str}"

    def _bfs_build_resource_pool(self, ctx) -> list:
        pool = []
        for i, current_url in enumerate(ctx.focused_urls_list):
            if settings.debug:
                sys.stderr.write(f"\n{'='*60}\n")
                sys.stderr.write(f"[BFS _bfs_build_resource_pool] BFS URL] Processing URL {i}: {current_url}\n")
            res = self._get_share_detail_with_qas_pattern(ctx, current_url)
            if settings.debug:
                sys.stderr.write(f"[BFS DEBUG] get_share_detail success={res.get('success')} url={current_url}\n")
                if not res.get("success"):
                    sys.stderr.write(f"[BFS DEBUG] get_share_detail failed: {res}\n")
                else:
                    sys.stderr.write(f"[BFS DEBUG] root list len={len(res.get('data', {}).get('list', []))}\n")
            if not res.get("success"):
                continue
            if settings.debug:
                sys.stderr.write(f"\nBFS _bfs_build_resource_pool] [BFS URL] {current_url}\n")
                for f in res.get("data", {}).get("list", []):
                    name = f.get("file_name", "")
                    if not re.search(r"\.(?:mp4|mkv)$", name, re.I):
                        continue
                    m = re.search(r"[Ee](\d+)", name)
                    ep_num = int(m.group(1)) if m else 0
                    in_target = ep_num in ctx.target_save_eps_set and ep_num <= ctx.advisor_confirmed_max_ep
                    marker = "→" if in_target else "×"
                    sys.stderr.write(self._format_bfs_file_preview_line(f, marker) + "\n")

            base_sid, stoken, best_url = (
                current_url.split("/s/")[1].split("#")[0],
                res.get("data", {}).get("stoken", ""),
                current_url,
            )

            all_nodes = {}
            sibling_idx_map = {}
            queue = [(current_url, None, None, str(i))]
            while queue:
                node_url, parent, dir_name, node_key = queue.pop(0)
                node_res = self._get_share_detail_with_qas_pattern(ctx, node_url, stoken=stoken)
                if not node_res.get("success"):
                    continue
                node_file_list = node_res.get("data", {}).get("list", [])
                all_nodes[node_url] = {
                    "raw_res": node_res,
                    "file_list": node_file_list,
                    "parent": parent,
                    "dir_name": dir_name,
                    "node_key": node_key,
                }
                if settings.debug:
                    node_file_count = len([f for f in node_file_list if not f.get("dir")])
                    node_dir_count = len([f for f in node_file_list if f.get("dir")])
                    node_name = dir_name if dir_name else "-"
                    if node_file_count == 0 and node_dir_count == 0:
                        sys.stderr.write(
                            f"[_bfs_build_resource_pool] BFS Step1: 目录节点编号: {node_key}\t目录名: '{node_name}'\t(空)\n"
                        )
                    else:
                        sys.stderr.write(
                            f"[_bfs_build_resource_pool] BFS Step1: 目录节点编号: {node_key}\t目录名: '{node_name}'\t文件: {node_file_count}个\t子目录: {node_dir_count}个\n"
                        )
                sub_dirs = [f for f in node_file_list if f.get("dir")]
                sibling_idx_map[node_url] = 0
                for f in sub_dirs:
                    sub_url = f"https://pan.quark.cn/s/{base_sid}#/list/share/{f.get('fid')}"
                    sub_name = f.get("file_name", "?")
                    sibling_idx = sibling_idx_map[node_url] + 1
                    sibling_idx_map[node_url] = sibling_idx
                    sub_key = f"{node_key}.{sibling_idx}"
                    queue.append((sub_url, node_url, sub_name, sub_key))

            all_eps = set()
            best_res = res
            ep_first_found = {}
            merged_ep_count = {}
            url_ep_counts = {}
            valid_node_infos = []
            best_url = current_url

            for node_url, node_data in all_nodes.items():
                file_list = node_data["file_list"]
                node_key = node_data.get("node_key", "?")
                node_name = node_data.get("dir_name") or "-"

                node_max_ep, _ = scan_quark_userlocal_max_ep(file_list)
                if settings.debug:
                    passed = "✓" if node_max_ep >= ctx.advisor_confirmed_max_ep else "✗"
                    decision = "通过" if node_max_ep >= ctx.advisor_confirmed_max_ep else "跳过"
                    sys.stderr.write(
                        f"[_bfs_build_resource_pool] BFS Step2: {passed}\t目录节点编号: {node_key}\t目录名: {node_name}\tmax_ep: {node_max_ep}\t目标: {ctx.advisor_confirmed_max_ep}\t{decision}\n"
                    )
                if node_max_ep < ctx.advisor_confirmed_max_ep:
                    continue

                check_result = self._check_share_detail_with_specific_rules(ctx, node_url, node_data)
                if not check_result["ok"]:
                    if settings.debug:
                        signals = check_result.get("signals", {})
                        dup_eps = check_result["details"].get("dup_eps", {})
                        signal_cn = {
                            "含数字方括号文件名": signals.get("has_numeric_bracket_filename"),
                            "预览重复剧集数": signals.get("preview_duplicate_ep_count"),
                        }
                        dup_ep_preview_items = check_result["details"].get("dup_ep_preview_items", {})
                        rule_name_cn = {
                            "NUMERIC_BRACKET_FILENAME_AND_PREVIEW_DUP_EP": "数字方括号文件名且QAS预览重复剧集",
                        }.get(check_result["rule_name"], check_result["rule_name"])
                        sys.stderr.write(
                            f"[_bfs_build_resource_pool] 节点体检失败：跳过节点={node_key} 目录={node_name} "
                            f"规则={check_result['rule_code']}/{rule_name_cn} "
                            f"可疑条件={signal_cn} 重复剧集={list(dup_eps.keys())}\n"
                        )
                        for ep, files in dup_ep_preview_items.items():
                            sys.stderr.write(f"[_bfs_build_resource_pool] 体检详情 EP{ep}:\n")
                            for f in files[:5]:
                                sys.stderr.write(self._format_bfs_file_preview_line(f, "-") + "\n")
                    continue

                node_eps, _, node_info, node_fm, node_ep_count = self._evaluate_files_for_target(
                    ctx,
                    file_list,
                    node_url,
                    show_debug=(node_url == current_url),
                )

                if not node_eps:
                    continue

                valid_node_infos.append({
                    "node_key": node_key,
                    "node_name": node_name,
                    "eps": sorted(node_eps),
                })

                for ep, cnt in node_ep_count.items():
                    merged_ep_count[ep] = max(merged_ep_count.get(ep, 0), cnt)
                for ep in node_eps:
                    should_sub = (
                        ep not in ep_first_found
                        or (
                            ctx.ep_selection_policy == "prefer_size"
                            and node_info[ep]["size"] < ep_first_found[ep]["size"]
                        )
                        or (
                            ctx.ep_selection_policy != "prefer_size"
                            and node_info[ep]["size"] > ep_first_found[ep]["size"]
                        )
                    )
                    if should_sub:
                        ep_first_found[ep] = {**node_info[ep], **node_fm[ep]}
                all_eps |= node_eps
                url_ep_counts[node_url] = len(node_eps)
                if url_ep_counts[node_url] > url_ep_counts.get(best_url, 0):
                    best_url = node_url

            if settings.debug:
                if not valid_node_infos:
                    sys.stderr.write(f"[_bfs_build_resource_pool] BFS Step2: 候选URL {i} 无有效节点（max_ep 未达标或 evaluate 无目标 EP）\n")
                else:
                    for info in valid_node_infos:
                        sys.stderr.write(
                            f"[_bfs_build_resource_pool] BFS Step2: 候选URL {i}，节点 {info['node_key']}({info['node_name']})，EP: {info['eps']} → 已加入候选池\n"
                        )
            if all_eps:
                pool.append(
                    {
                        "url": best_url,
                        "eps": all_eps,
                        "ep_first_found": ep_first_found,
                        "share_data": best_res,
                        "ep_count": merged_ep_count,
                    }
                )
            else:
                if settings.debug:
                    sys.stderr.write(f"[_bfs_build_resource_pool] URL {current_url} 淘汰（无有效 EP）\n")
        return pool

    def _build_dual_candidate_pools(self, ctx) -> tuple[dict, list]:
        regex_or_exact_batch_save_candidates = {}
        exact_one_by_one_candidates = []

        if settings.debug:
            sys.stderr.write(
                f"[_build_dual_candidate_pools] [POOL BUILD] attainable_to_save_eps_set={sorted(ctx.attainable_to_save_eps_set)}\n"
            )
            for i, entry in enumerate(ctx.bfs_result_pool):
                sys.stderr.write(
                    f"[_build_dual_candidate_pools] [POOL BUILD] pool[{i}] eps={sorted(entry.get('eps', []))} url={entry.get('url', '')[:60]}\n"
                )
        for ep in sorted(ctx.attainable_to_save_eps_set):
            if settings.debug:
                sys.stderr.write(f"[_build_dual_candidate_pools] [POOL BUILD] processing ep={ep}\n")
            for i, entry in enumerate(ctx.bfs_result_pool):
                if ep not in entry.get("eps", []):
                    if settings.debug:
                        sys.stderr.write(
                            f"[_build_dual_candidate_pools] [POOL BUILD] ep={ep} pool[{i}] eps={sorted(entry.get('eps', []))} → NOT in entry, skipped\n"
                        )
                    continue
                cand = {"url_idx": i, **entry["ep_first_found"][ep]}
                if settings.debug:
                    sys.stderr.write(
                        f"[_build_dual_candidate_pools] [POOL BUILD] ep={ep} pool[{i}] cand_url={cand.get('url', '')[:60]} size={cand.get('size')}\n"
                    )
                ep_cnt = entry["ep_count"].get(ep, 1)
                cand_score = self._calc_candidate_score(cand, ctx.ep_selection_policy)

                if ep_cnt == 1:
                    # regex 属性候选者
                    # Step 1: 先比 exact 池
                    exact_same_ep = [x for x in exact_one_by_one_candidates if x["ep"] == ep]
                    if exact_same_ep:
                        exact_cand = exact_same_ep[0]
                        # regex 的 prefer >= exact 的 prefer → 踢出 exact 池该 EP
                        if cand_score >= exact_cand["score"]:
                            exact_one_by_one_candidates.remove(exact_cand)
                        else:
                            # regex 不如 exact，淘汰
                            continue

                    # Step 2: 再走 regex 池 PK 算法
                    if ep not in regex_or_exact_batch_save_candidates:
                        regex_or_exact_batch_save_candidates[ep] = [cand]
                    else:
                        existing = regex_or_exact_batch_save_candidates[ep]
                        # 同 EP 比 prefer：prefer_quality 选最大 size，prefer_size 选最小 size
                        best_size = max(c["size"] for c in existing) if ctx.ep_selection_policy != "prefer_size" else min(c["size"] for c in existing)
                        if ctx.ep_selection_policy == "prefer_size":
                            best_size = min(c["size"] for c in existing)
                        else:
                            best_size = max(c["size"] for c in existing)
                        ws = [c for c in existing if c["size"] == best_size]
                        if len(ws) > 1:
                            bm = max(c["mtime"] for c in ws)
                            ws = [c for c in ws if c["mtime"] == bm]
                        existing_best = ws[0]
                        # 比 best：cand 赢了就替换，输了就淘汰
                        cand_is_better = (
                            ctx.ep_selection_policy == "prefer_size" and cand["size"] < existing_best["size"]
                        ) or (
                            ctx.ep_selection_policy != "prefer_size" and cand["size"] > existing_best["size"]
                        ) or (
                            cand["size"] == existing_best["size"] and cand["mtime"] > existing_best["mtime"]
                        )
                        if cand_is_better:
                            regex_or_exact_batch_save_candidates[ep] = [cand]
                        # else: 淘汰，不入池
                else:
                    # exact 属性候选者（ep_cnt > 1）,同目录下重复剧集 xx.S01E08.4k.mkv. xx.s01e08.hdr.mkv.
                    # Step 1: 先比 regex 池
                    regex_same_ep = regex_or_exact_batch_save_candidates.get(ep, [])
                    if regex_same_ep:
                        regex_best = regex_same_ep[0]
                        # exact 的 prefer > regex 的 prefer → 踢出 regex 池该 EP
                        if cand_score > self._calc_candidate_score(
                            regex_best, ctx.ep_selection_policy
                        ):
                            regex_or_exact_batch_save_candidates.pop(ep, None)
                        else:
                            # exact 不如 regex，淘汰
                            continue

                    # Step 2: 再走 exact 池自身 PK
                    exact_same_ep = [x for x in exact_one_by_one_candidates if x["ep"] == ep]
                    if exact_same_ep:
                        existing = exact_same_ep[0]
                        # exact 的 prefer > 池内 EP 的 prefer → 替换
                        if cand_score > existing["score"]:
                            exact_one_by_one_candidates.remove(existing)
                            exact_one_by_one_candidates.append(
                                {
                                    "ep": ep,
                                    "best_candidate": cand,
                                    "pool_entry": entry,
                                    "score": cand_score,
                                }
                            )
                    else:
                        # 直接入池
                        exact_one_by_one_candidates.append(
                            {
                                "ep": ep,
                                "best_candidate": cand,
                                "pool_entry": entry,
                                "score": cand_score,
                            }
                        )

        if settings.debug:
            batch_by_url, onebyone_by_url = {}, {}
            for ep, cands in regex_or_exact_batch_save_candidates.items():
                uidx = cands[0].get("url_idx", -1)
                batch_by_url.setdefault(uidx, []).append(ep)
            for item in exact_one_by_one_candidates:
                uidx = item.get("best_candidate", {}).get("url_idx", -1)
                onebyone_by_url.setdefault(uidx, []).append(item["ep"])
            sys.stderr.write("\n")
            for i, entry in enumerate(ctx.bfs_result_pool):
                url_short = entry["url"][-40:]
                b_eps = sorted(batch_by_url.get(i, []))
                o_eps = sorted(onebyone_by_url.get(i, []))
                sys.stderr.write(
                    f"[_build_dual_candidate_pools] pool[{i}] url={url_short} | regex_or_exact_batch_save_candidates={b_eps} | 逐一转存模式队列(exact_one_by_one_candidates)={o_eps}\n"
                )
            sys.stderr.write(
                f"[_build_dual_candidate_pools] [PK SUMMARY] regex_or_exact_batch_save_candidates={sorted(regex_or_exact_batch_save_candidates.keys())} | 逐一转存模式队列(exact_one_by_one_candidates)={sorted([item['ep'] for item in exact_one_by_one_candidates])}\n"
            )

        return regex_or_exact_batch_save_candidates, exact_one_by_one_candidates

    def _execute_phase1_phase2_quark_auto_save(self, ctx) -> set:
        remaining_regex_or_exact_batch_save_candidates = set(
            ctx.regex_batch.keys()
        )
        actual_saved_eps_set = set()

        # Step 1: 按 cand_url 分组（用 remaining 而非 ctx.regex_batch.keys()）
        batch_by_url = {}  # {cand_url: [(ep, cand), ...]}
        for ep in list(remaining_regex_or_exact_batch_save_candidates):
            cands = ctx.regex_batch.get(ep, [])
            if not cands:
                continue
            cand = cands[0]
            batch_by_url.setdefault(cand["url"], []).append((ep, cand))

        # Step 2: 每个 cand_url 调一次 run_script_now
        for cand_url, ep_cand_list in batch_by_url.items():
            batch_eps = [ep for ep, _ in ep_cand_list]

            # 用 cand["filename"] 生成精确 pattern
            exact_parts = []
            for ep, cand in ep_cand_list:
                fname = cand.get("filename", "")
                if fname:
                    exact_parts.append(f"^{re.escape(fname)}$")
                else:
                    exact_parts.append(self.ep_filename_regex_template.replace("{ep}", f"(0*{ep})"))
            smart_pattern = "|".join(exact_parts)

            if settings.debug:
                sys.stderr.write(f"[Phase1 DEBUG] cand_url={cand_url} batch_eps={sorted(batch_eps)}\n")
                sys.stderr.write(f"[Phase1 DEBUG] smart_pattern={smart_pattern}\n")

            ctx.target_task.update({
                "pattern": smart_pattern,
                "shareurl": cand_url,
                "shareurl_ban": "",
                "status_code": 0,
                "startfid": "",
                "enddate": "",
            })
            self.client.update_task_qas_config(ctx.target_task)

            target_task_run = ctx.target_task.copy()
            for key in ["runweek"]:
                target_task_run.pop(key, None)
            if "addition" in target_task_run:
                strm = target_task_run["addition"].get("alist_strm_gen", {})
                target_task_run["addition"] = {"alist_strm_gen": strm}

            try:
                result_text = self.client.run_script_now([target_task_run])
            except QasApiError as e:
                self.logger.error(f"[Phase1 ERROR] QasApiError: {e}")
                continue

            for line in result_text.split("\n"):
                if line.strip():
                    self.logger.info(f"[SSE] {line}")

            parsed = self._parse_sse_result(result_text)
            actual_saved_eps = set(parsed.get("episodes", []))
            actual_saved_eps_set |= actual_saved_eps

            # 保持 remaining 扣减逻辑
            remaining_regex_or_exact_batch_save_candidates -= actual_saved_eps

        # ========== DEBUG: Phase1 结束，Phase2 开始 ==========
        if settings.debug:
            sys.stderr.write(f"\n[Phase1 DEBUG] Phase1 结束: actual_saved_eps_set={sorted(actual_saved_eps_set)} remaining={sorted(remaining_regex_or_exact_batch_save_candidates)}\n")
            sys.stderr.write(f"[Phase1 DEBUG] Phase2 开始: exact_one len={len(ctx.exact_one)}\n")
        # ========== DEBUG END ==========

        if not ctx.exact_one:
            if settings.debug:
                self.logger.info(
                    "\n\n[QAS转存Phase2-逐一转存DEBUG] Phase2 转存模式，无任务（逐一转存模式队列(exact_one_by_one_candidates) 为空）"
                )
            else:
                self.logger.info(
                    f"\n\n[QAS转存Phase2-逐一转存DEBUG] 开始 exact 阶段，已完成转存(actual_saved_eps_set): {sorted(actual_saved_eps_set)}, 逐一转存模式队列(exact_one_by_one_candidates) ep={[item['ep'] for item in ctx.exact_one]}"
                )
        for item in ctx.exact_one:
            ep = item["ep"]
            pool_entry = item["pool_entry"]
            # url 优先用 best_candidate["url"]，防御字段缺失
            url = item.get("best_candidate", {}).get("url") or pool_entry["url"]
            fname = item.get("best_candidate", {}).get("filename", "")
            exact_pattern = f"^{re.escape(fname)}$" if fname else self.ep_filename_regex_template.replace("{ep}", f"(0*{ep})")

            # ========== DEBUG: Phase2 每次循环 ==========
            if settings.debug:
                best_cand_url = item.get("best_candidate", {}).get("url", "")
                best_cand_fn = fname
                sys.stderr.write(f"[Phase2 DEBUG] ep={ep} pool_entry_url={pool_entry['url']} best_cand_url={best_cand_url} best_cand_fn={best_cand_fn}\n")
            # ========== DEBUG END ==========

            if settings.debug:
                self.logger.info(f"🚀 [Phase2 exact] 精确下载 EP{ep}: {exact_pattern}")

            ctx.target_task.update(
                {
                    "pattern": exact_pattern,
                    "shareurl": url,
                    "shareurl_ban": "",
                    "status_code": 0,
                    "startfid": "",
                    "enddate": "",
                }
            )
            self.client.update_task_qas_config(ctx.target_task)

            target_task_run = ctx.target_task.copy()
            for key in ["runweek"]:
                target_task_run.pop(key, None)
            if "addition" in target_task_run:
                strm = target_task_run["addition"].get("alist_strm_gen", {})
                target_task_run["addition"] = {"alist_strm_gen": strm}

            if self.client.supports_run_scp_json:
                result = self.client.run_script_now([target_task_run])
                for log_line in result.get("logs", []):
                    self.logger.debug(f"[JSON API exact] {log_line}")
                for task_result in result.get("tasks", []):
                    if task_result.get("taskname") != ctx.target_task.get("taskname"):
                        continue
                    renamed_list = task_result.get("file_name_re", [])
                    if not renamed_list:
                        self.logger.warning(
                            f"[JSON exact] taskname={task_result.get('taskname')} 无 file_name_re，可能下载失败"
                        )
                        continue
                    for renamed in renamed_list:
                        m = re.search(r"(?i)(?:E|第)\s*0*(\d+)", renamed)
                        if m and int(m.group(1)) == ep:
                            actual_saved_eps_set.add(ep)
            else:
                done = False  # noqa: F841
                self.logger.info(
                    f"[SSE] run_script_now 返回报文 === Phase2 exact 开始，target EP={ep} ==="
                )
                try:
                    result_text = self.client.run_script_now([target_task_run])
                except QasApiError as e:
                    self.logger.error(f"[Phase2 ERROR] QasApiError: {e}")
                    continue

                for line in result_text.split("\n"):
                    if line.strip():
                        self.logger.info(f"[SSE] {line}")
                        if "程序结束" in line or "运行时长" in line:
                            done = True  # noqa: F841

                # CLI 用 _parse_sse_result 解析业务数据
                parsed = self._parse_sse_result(result_text)
                for saved_ep in parsed.get("episodes", []):
                    actual_saved_eps_set.add(saved_ep)

                self.logger.info(
                    f"\n\n[QAS转存Phase2-逐一转存DEBUG] exact 阶段结束，已完成转存(actual_saved_eps_set): {sorted(actual_saved_eps_set)}"
                )

        if not actual_saved_eps_set:
            self.logger.warning(
                f"[三轮] 夸克转存失败，恢复原始URL: {ctx.task_config_profile['original_shareurl'][:50]}..."
            )
            ctx.target_task.update(
                {
                    "pattern": ctx.task_config_profile["original_pattern"],
                    "shareurl": ctx.task_config_profile["original_shareurl"],
                    "startfid": ctx.task_config_profile["original_startfid"],
                    "shareurl_ban": "",
                }
            )
            self.client.update_task_qas_config(ctx.target_task)

        return actual_saved_eps_set

    def _finalize_throne_merge_task(self, ctx) -> None:
        throne_ep = (
            max(ctx.actual_saved_eps_set | {ctx.quark_userlocal_max_ep})
            if (ctx.quark_userlocal_max_ep > 0 or ctx.actual_saved_eps_set)
            else 0
        )
        cands_max = []
        for i, entry in enumerate(ctx.bfs_result_pool):
            if throne_ep in entry["eps"]:
                cands_max.append({"url_idx": i, **entry["ep_first_found"][throne_ep]})

        best_u_idx = 0
        if cands_max:
            bs = (
                min(c["size"] for c in cands_max)
                if ctx.ep_selection_policy == "prefer_size"
                else max(c["size"] for c in cands_max)
            )
            ws = [c for c in cands_max if c["size"] == bs]
            if len(ws) > 1:
                bm = max(c["mtime"] for c in ws)
                ws = [c for c in ws if c["mtime"] == bm]
            best_u_idx = ws[0]["url_idx"]

        best_ent = ctx.bfs_result_pool[best_u_idx]
        max_mt, final_sfid = 0, ctx.task_config_profile["original_startfid"]
        for info in best_ent["ep_first_found"].values():
            if info.get("mtime", 0) > max_mt:
                max_mt, final_sfid = info.get("mtime", 0), info.get("fid", "")

        ctx.target_task.update(
            {
                "pattern": ctx.task_config_profile["original_pattern"],
                "shareurl": best_ent["url"],
                "startfid": final_sfid,
                "shareurl_ban": "",
            }
        )
        self.client.update_task_qas_config(ctx.target_task)

    def _do_smart_update_single(
        self, task, ep_selection_policy, update_mode, input_url, add_prompt=""
    ):
        # Phase 1: Create ctx, map all inputs
        from types import SimpleNamespace

        ctx = SimpleNamespace()
        ctx.task = task
        ctx.ep_selection_policy = ep_selection_policy
        ctx.update_mode = update_mode
        ctx.input_url = input_url
        ctx.add_prompt = add_prompt

        ctx.target_task = ctx.task
        self._resolve_task_meta(ctx)
        ctx.target_task_name_kword = self._normalize_taskname(ctx.task_config_profile["taskname"])
        self._scan_quark_userlocal_directory(ctx)

        ctx.advisor_kind = ctx.focused_urls_index = ctx.advisor_max_ep = None

        # 先打印 header（显示本地状态）
        self._log_task_header(ctx)

        if ctx.input_url:
            self._get_focused_urls_from_manual_url(ctx)
        else:
            self._get_focused_urls_via_search(ctx)
            if getattr(ctx, "early_return", None):
                return ctx.early_return

        # 去重并插入原始 shareurl
        self._dedup_insert_original_shareurl(ctx)

        if not ctx.focused_urls_list:
            raise Exception(f"No valid links selected for 【{ctx.task_config_profile['taskname']}】.")

        # header 已在搜索前打印（见上方），此处不再重复

        ctx.target_regex = self._build_target_save_eps_and_selected_regex(ctx)

        if not ctx.target_save_eps_set:
            return self._empty_target_save_eps_return(ctx)

        ctx.bfs_result_pool = self._bfs_build_resource_pool(ctx)
        if not ctx.bfs_result_pool:
            return self._no_candidate_return(ctx)

        ctx.hole_for_planned = {
            e
            for e in range(ctx.quark_userlocal_min_ep, ctx.quark_userlocal_max_ep + 1)
            if e not in ctx.quark_userlocal_owned_eps_set
        }
        ctx.attainable_to_save_eps_set = self._recompute_planned_save_eps_after_pool(ctx)
        if settings.debug:
            all_pool_eps = set()
            for entry in ctx.bfs_result_pool:
                all_pool_eps |= entry["eps"]
            new_base = {e for e in all_pool_eps if e > ctx.quark_userlocal_max_ep}
            sys.stderr.write(
                f"[app.py:934] [POOL→planned] all_pool_eps={sorted(all_pool_eps)} quark_local_max={ctx.quark_userlocal_max_ep} new_base={sorted(new_base)} hole_set={sorted(ctx.hole_for_planned)} update_mode={ctx.update_mode} planned={sorted(ctx.attainable_to_save_eps_set)}\n"
            )

        ctx.regex_batch, ctx.exact_one = self._build_dual_candidate_pools(ctx)

        ctx.actual_saved_eps_set = self._execute_phase1_phase2_quark_auto_save(ctx)

        # 判断 transfer_failed（不改函数签名）
        transfer_failed = bool(ctx.target_save_eps_set) and not ctx.actual_saved_eps_set

        if transfer_failed:
            return {
                "success": False,
                "transfer_failed": True,
                "msg": "转存失败",
                "new_eps": [],
                "filled_eps": [],
                "target_save_eps_set": ctx.target_save_eps_set,
                "advisor_kind": ctx.advisor_kind,
                "focused_urls_index": ctx.focused_urls_index,
                "advisor_max_ep": ctx.advisor_max_ep,
                "target_task": ctx.target_task,
                "target_task_name_kword": ctx.target_task_name_kword,
                "snapshot_before": ctx.snapshot_before,
            }

        self._finalize_throne_merge_task(ctx)

        return {
            "success": True,
            "msg": "处理完成",
            "new_eps": [e for e in ctx.actual_saved_eps_set if e > ctx.quark_userlocal_max_ep],
            "filled_eps": [
                e for e in ctx.actual_saved_eps_set if e <= ctx.quark_userlocal_max_ep
            ],
            "target_save_eps_set": ctx.target_save_eps_set,
            "advisor_kind": ctx.advisor_kind,
            "focused_urls_index": ctx.focused_urls_index,
            "advisor_max_ep": ctx.advisor_max_ep,
            "target_task": ctx.target_task,
            "target_task_name_kword": ctx.target_task_name_kword,
            "snapshot_before": ctx.snapshot_before,
        }

    def _save_report(self, run_results):
        """生成极简全中文报告"""
        report_dir = os.path.expanduser(settings.report_dir)
        os.makedirs(report_dir, exist_ok=True)
        status_map = {"success": "成功", "no_update": "无更新", "no_candidate": "候选失效", "failed": "失败", "transfer_failed": "转存失败"}
        msg_map = {
            "No valid episodes": "未发现有效剧集",
            "OK": "处理完成",
            "No valid episodes...": "系统未发现匹配资源",
            "没有新剧集": "没有新剧集",
        }
        snapshot = run_results[0].get("snapshot_before", {"max": 0, "min": 0, "hole": [], "last_updated": 0}) if run_results else {"max": 0, "min": 0, "hole": [], "last_updated": 0}
        m, min_val, hole, last_updated = (
            snapshot["max"],
            snapshot["min"],
            snapshot["hole"],
            snapshot.get("last_updated", 0),
        )
        miss_str = "无缺集" if not hole else f"缺失: {hole}"
        updated_str = f"（更新于：{simple_relative_day(last_updated)}）" if last_updated else ""
        results = []
        for r in run_results:
            status_cn = status_map.get(r["status"], r["status"])
            advisor_info = r.get("advisor_info")
            entry = {
                "更新订阅": r.get("taskname"),
                "顾问决策": {
                    "顾问类型": advisor_info.get("advisor_kind") if advisor_info else None,
                    "资源选择": [i+1 for i in (advisor_info.get("focused_urls_index") or [])] if advisor_info else None,
                    "最新剧集": [advisor_info.get("advisor_max_ep")] if advisor_info and advisor_info.get("advisor_max_ep") is not None else None,
                } if advisor_info else None,
                "更新执行": {
                    "结果": status_cn,
                    "新剧集": r.get("new_episodes", []),
                    "已补剧集": r.get("filled_episodes", []),
                },
            }
            if status_cn != "成功":
                entry["更新执行"]["消息"] = msg_map.get(r["message"], r["message"])
            results.append(entry)
        report = {
            "订阅": r.get("target_task_name_kword") or "未命名",
            "当前网盘状态": {
                "最高集数": m,
                "最低集数": min_val,
                "描述": f"💾 网盘：最高 {m}{updated_str}，最低 {min_val}，{miss_str}",
            },
            "片源倾向": get_policy_desc(settings.ep_selection_policy),
            "执行结果": results,
            "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        txt_fname = f"qas_lazy_{r.get('target_task_name_kword', 'unknown')}_{ts}.txt"


        # 写格式化 txt
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        policy = get_policy_desc(settings.ep_selection_policy)

        # 从 results 提取第一条（单订阅场景）
        r = run_results[0] if run_results else {}
        status_cn = status_map.get(r.get("status", ""), r.get("status", ""))
        advisor_info = r.get("advisor_info") or {}
        new_eps = r.get("new_episodes", [])
        filled_eps = r.get("filled_episodes", [])
        new_count = len(new_eps)
        patch_count = len(filled_eps)
        result_emoji = "✨" if status_cn == "成功" else ("⚠️" if status_cn == "候选失效" else "🔕")
        miss_display = f"✨ 无缺集" if not hole else f"📉 缺失：{sorted(hole)}"

        # 顾问字段（--url 模式无顾问，显示为 -）
        advisor_kind = advisor_info.get("advisor_kind") or "-"
        focused_urls_index = advisor_info.get("focused_urls_index") or []
        advisor_max_ep = advisor_info.get("advisor_max_ep") or "-"
        resource_indices = ", ".join(str(i+1) for i in focused_urls_index)
        target_save_eps_set = r.get("target_save_eps_set") or set()

        txt_lines = [
            f"📊 订阅报告 - {r.get('target_task_name_kword', '未知')}",
            "─" * 32,
            "📺 剧集状态：",
            f"   • 最高：第 {m} 集{updated_str}",
            f"   • 最低：第 {min_val} 集",
            f"   • 完整度：{miss_display}",
            "",
            f"🎯 订阅目标：{sorted(target_save_eps_set)}",
            f"💎 画质偏好：{policy}",
        ]

        if advisor_kind != "-":
            txt_lines.extend([
                "",
                "🤖 顾问决策：",
                f"   • 顾问类型：{advisor_kind}",
                f"   • 资源选择：{resource_indices or '-'}",
                f"   • 最新剧集：{advisor_max_ep}",
            ])

        msg = r.get("message") or ""
        txt_lines.extend([
            "",
            "📈 本次更新：",
            f"   • 结果：{result_emoji} {status_cn}" + (f" {msg}" if msg and status_cn != "成功" else ""),
            f"   • 新增：{new_count} 集" + (f" {new_eps}" if new_eps else ""),
            f"   • 补档：{patch_count} 集" + (f" {filled_eps}" if filled_eps else ""),
            "─" * 32,
            f"🕐 报告时间：{timestamp}",
        ])

        with open(f"{report_dir}/{txt_fname}", "w", encoding="utf-8") as f:
            f.write("\n".join(txt_lines))

        self.logger.info("\n\n" + "\n".join(txt_lines))

    def generate_exact_filename_regex(self, ep, pool_entry):
        """生成精确文件名正则，只匹配已确认的最优文件，防同 EP 多格式重复下载"""
        ep_info = pool_entry["ep_first_found"].get(ep)
        if not ep_info:
            return ""
        fname = ep_info.get("filename", "")
        if not fname:
            return ""
        return f"^{re.escape(fname)}$"

    def _log_task_header(self, ctx) -> None:
        """打印任务状态头，所有信息从 ctx 读取"""
        disk = DiskStatus(
            quark_userlocal_owned_eps_set=ctx.quark_userlocal_owned_eps_set,
            quark_userlocal_min_ep=ctx.quark_userlocal_min_ep,
            quark_userlocal_max_ep=ctx.quark_userlocal_max_ep,
            quark_userlocal_hole_eps_set=set(ctx.quark_userlocal_hole_eps_set),
            last_updated=ctx.max_ep_mtime,
        )
        goal = SearchGoal(
            mode=ctx.update_mode,
            suggested_max_ep=getattr(ctx, 'advisor_confirmed_max_ep', 0) or 0,
        )
        disk_str = f"最高第{disk.quark_userlocal_max_ep}集"
        if disk.last_updated:
            disk_str += f"（更新于：{simple_relative_day(disk.last_updated)}）"
        disk_str += f"，最低第{disk.quark_userlocal_min_ep}集"
        if not disk.quark_userlocal_owned_eps_set:
            disk_str = "空目录"
        disk_str += (
            f"，缺 {sorted(disk.quark_userlocal_hole_eps_set)}"
            if disk.quark_userlocal_hole_eps_set
            else "，无缺集"
        )
        goal_str = (
            "尽可能获取全部发布的剧集"
            if not disk.quark_userlocal_owned_eps_set
            else f"第{disk.quark_userlocal_max_ep}集以上资源"
            + (
                "，补全缺失剧集"
                if goal.mode == "all" and disk.quark_userlocal_hole_eps_set
                else ""
            )
        )
        dup_str = (
            f" ⚠️  发现重复剧集：{' '.join(f'[{ep}集 {ctx.eps_count[ep]}个文件]' for ep in sorted(ctx.dup_eps))}"
            if ctx.dup_eps
            else ""
        )
        ep0_str = (
            f" ⚠️  发现 {len(ctx.ep0_files)} 个无效 EP0 文件：{', '.join(html.unescape(name) for name, _ in ctx.ep0_files[:10])}{'...' if len(ctx.ep0_files) > 10 else ''}"
            if ctx.ep0_files
            else ""
        )
        unmatch_str = (
            f" ⚠️  发现 {len(ctx.unmatch_files)} 个文件不符合命名模板：{', '.join(html.unescape(f) for f in ctx.unmatch_files[:10])}{'...' if len(ctx.unmatch_files) > 10 else ''}"
            if ctx.unmatch_files
            else ""
        )
        sep = "=" * 60
        header = (
            f"\n{sep}\n"
            f"  📋 订阅：{ctx.task_config_profile['taskname']}\n"
            f"  💾 当前：{disk_str}{dup_str}{ep0_str}{unmatch_str}\n"
            f"  🎯 目标：{goal_str}\n"
            f"  💎 偏好：{get_policy_desc(ctx.ep_selection_policy)}\n\n{sep}"
        )
        print(header)

    def _handle_update_command(
        self, identifier, url=None, update_mode="new", add_prompt=""
    ):
        policy = settings.ep_selection_policy
        try:
            # 按 keyword 查找任务
            task = self.client.get_task_by_keyword(identifier)
            if not task:
                print(f"未找到匹配的任务: {identifier}")
                return
            # 生成搜索关键词（只写一次，之后只读）
            run_results = []
            try:
                res = self._do_smart_update_single(
                    task, policy, update_mode, url, add_prompt
                )
                # 保存更新后的任务（从 ctx 返回的 target_task）
                self.client.update_task_qas_config(res.get("target_task"))
                if res.get("transfer_failed"):
                    status = "transfer_failed"
                elif res.get("no_candidate"):
                    status = "no_candidate"
                elif res.get("no_update"):
                    status = "no_update"
                else:
                    status = "success"
                run_results.append(
                    {
                        "taskname": task.get("taskname"),
                        "status": status,
                        "new_episodes": res.get("new_eps", []),
                        "filled_episodes": res.get("filled_eps", []),
                        "message": res.get("msg"),
                        "target_save_eps_set": res.get("target_save_eps_set"),
                        "advisor_info": {
                            "advisor_kind": res.get("advisor_kind"),
                            "focused_urls_index": res.get("focused_urls_index"),
                            "advisor_max_ep": res.get("advisor_max_ep"),
                        } if res.get("advisor_kind") else None,
                        "target_task_name_kword": res.get("target_task_name_kword"),
                        "snapshot_before": res.get("snapshot_before"),
                        "target_task": res.get("target_task"),
                    }
                )
            except ValueError as e:
                print(f"参数错误: {e}")
                return
            except Exception as e:
                err = str(e)
                if "NOT_FAILED_JUST_NO_UPDATE|" in err:
                    err_msg = err.split("|")[-1]
                    run_results.append(
                        {
                            "taskname": task.get("taskname"),
                            "status": "no_candidate",
                            "message": f"候选资源失效：{err_msg}",
                            "target_task_name_kword": task.get("taskname"),
                        }
                    )
                else:
                    run_results.append(
                        {
                            "taskname": task.get("taskname"),
                            "status": "failed",
                            "message": str(e),
                        }
                    )
            self._save_report(run_results)
        except Exception as e:
            print(f"CLI 运行错误: {e}")


if __name__ == "__main__":
    cli = QasLazyCli()
    if len(sys.argv) > 2:
        cli._handle_update_command(sys.argv[2], update_mode=sys.argv[1])
