# 实现session和session manager

# session 主要负责单个会话的持久化和回复，消息检索存储，基本的元信息管理，不触碰数据本身的操作逻辑
# session manager 则负责管理一个会话的生命周期和状态管理，创建，读取，删除，获取状态等
from typing import List
from datetime import datetime
from typing import Literal
from pathlib import Path
from contextlib import suppress
from collections import defaultdict
import json
import os  # 用于将tmp替换原始文件

from wiki_agent.utils.helpers import (
    ensure_dir,
)

from wiki_agent.message import Message, find_first_legal_idx
from wiki_agent.log import get_logger

logger=get_logger("SESSION")

class Session:
    def __init__(self,key):
        # 身份/元信息
        self.key = key # 我是谁，标识

        self.created_at=datetime.now().isoformat()
        self.updated_at=datetime.now().isoformat()

        # 状态信息
        self.session_title="未命名"
        self.status:Literal["active","closed"]="active"
        self.token_cost: dict = {"prompt": 0, "completion": 0, "total": 0}
        self.current_window_tokens:int=0
        self.last_consolidated:int=0
        self.last_summery:str=""

        # 内容信息
        self.history:List[Message]=[]

    def add_message(self,message:Message):
        self.history.append(message)

        # 状态更新
        self.updated_at=datetime.now().isoformat()

    def add_messages(self,messages:list[Message]):
        self.history.extend(messages)

        # 状态更新
        self.updated_at=datetime.now().isoformat()
    
    def get_history(self,max_messages_length:int=10,extend_to_user:bool=True):
        """
        选中一个满足最大长度，起始合法(不保证整个历史合法)的记录

        Args:
            max_message:窗口的最大长度
            extend_to_user:返回的第一条消息是否必须是user
        """
        if max_messages_length<=0:
            return []
        # 1. 选中可选的未压缩信息
        unconsolidated_messages=self.history[self.last_consolidated:]
        # 2. 额外遍历处理逻辑
        # 3. 检查信息长度，如果小于最大长度，直接进入步骤4
        limited_messages=[]
        if len(unconsolidated_messages)<max_messages_length:
            limited_messages=unconsolidated_messages
        # 3. 按照最大窗口进行截取
        else:
            limited_messages=unconsolidated_messages[-max_messages_length:]
        # 4. 检查修复不合法的对话
        start=find_first_legal_idx(limited_messages,extend_to_user)
        messages=limited_messages[start:]
        return messages

    def update_token_cost(self,prompt:int,completion:int,total:int):
        """
        更新token消耗信息
        """
        self.token_cost["prompt"]+=prompt
        self.token_cost["completion"]+=completion
        self.token_cost["total"]+=total

class SessionManager:
    def __init__(self,workspace:Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace/"sessions")
        self._cached_session={}

    def get_or_create(self,session_key:str)->Session:
        if  session_key in self._cached_session:
            return self._cached_session[session_key]
        
        session = self._load(session_key)

        if session is None:
            session = Session(key=session_key)

        self._cached_session[session_key]=session
        return session

    def _prase_checkpoint(self,file_path:Path):
        metadata:dict={}
        history=[]
        with open(file_path) as f:
            for line in f:
                try:
                    # 处理空行,空行会带"\n"，需要进行strip
                    if not line.strip():
                        continue

                    line_data=json.loads(line)
                    if "_type" in line_data:
                        metadata.update(line_data)
                        continue
                    else:
                        history.append(Message.model_validate(line_data))
                except Exception as e:
                    logger.warning(f"数据 {line[:50]} 处理失败，将被跳过 - {e}")
                    continue
        return metadata,history

    def _validate_meta_data(self,meta_data:dict):
        # key/last_consolidated 用 is None 判断（0 是合法值——空会话游标从 0 开始）
        for required in ("key", "last_consolidated"):
            if meta_data.get(required, None) is None:
                logger.warning("元信息损坏（缺 %s），将新建会话", required)
                return {}

        if not (meta_data.get("created_at") and meta_data.get("updated_at")):
            now=datetime.now().isoformat()
            meta_data["created_at"] = meta_data["updated_at"] = now

        if not meta_data.get("session_title",None):
            meta_data["session_title"]="未命名"

        if not meta_data.get("status",None):
            meta_data["status"]="active"

        if meta_data.get("last_summery", None) is None:
            meta_data["last_summery"] = ""

        if meta_data.get("current_window_tokens",None) is None:
            meta_data["current_window_tokens"]= 0

        if meta_data.get("token_cost",None) is None:
            logger.warning("token cost信息损坏，将从0计费")
            meta_data["token_cost"]={"prompt":0,"completion":0,"total":0}

        return meta_data

    def _load(self,session_key):
        # file_path-< session_dir+session_key
        file_path=self.sessions_dir/f"{session_key}.jsonl"
        # if 不存在 then: 
        if not file_path.exists():
        #   return None
            return None
        # else:
        else:
            meta_data,history = self._prase_checkpoint(file_path)

            # 分解文件，读取metadata，和content，重新组织session
            validate_meta_data=self._validate_meta_data(meta_data)
            if validate_meta_data:
                session=Session(key=meta_data["key"])
                session.history=history
                session.status=validate_meta_data["status"]
                session.created_at=validate_meta_data["created_at"]
                session.updated_at=validate_meta_data["updated_at"]
                session.last_consolidated=validate_meta_data["last_consolidated"]
                session.session_title=validate_meta_data["session_title"]
                session.last_summery=validate_meta_data["last_summery"]
                session.token_cost=validate_meta_data["token_cost"]
                session.current_window_tokens=validate_meta_data["current_window_tokens"]
                # 返回重新组织的结果
                return session
        return None

    def save_checkpoint(self,session:Session,fsync:bool=False):
        """
        将会话持久化到磁盘中，fsync为True表示立即将更新刷入磁盘，该操作较慢，默认为False。False时的做法是将内容写入操作系统的页缓存
        当操作系统关机的时候，会自动存入磁盘。但是如果遇到掉电等情况会丢失(因为是内存)
        """

        # 获取存储路径
        key=session.key
        if ensure_dir(self.sessions_dir):
            file_path=self.sessions_dir/f"{key}.jsonl"
            # 先创建临时文件，再写入
            tmp_file_path=file_path.with_suffix(".tmp")
            try:
                with open(tmp_file_path,"w",encoding="utf-8") as f:
                    # 写好meatadata
                    metadata_line={
                        "_type":"metadata",
                        "key":session.key,
                        "created_at":session.created_at,
                        "updated_at":session.updated_at,
                        "session_title":session.session_title,
                        "last_consolidated":session.last_consolidated,
                        "last_summery":session.last_summery,
                        "status":session.status,
                        "token_cost":session.token_cost,
                        "current_window_tokens":session.current_window_tokens
                    }
                    f.write(json.dumps(metadata_line,ensure_ascii=False)+ "\n")

                    # 将session中的记录转换为存储文件
                    for message in session.history:
                        # message还原成字典格式再写入,加\n换行
                        f.write(message.model_dump_json()+"\n")

                    # 立即同步到磁盘
                    if fsync:
                        f.flush()
                        os.fsync(f.fileno())
                    
                os.replace(tmp_file_path,file_path)

                # 目录本质上是特殊的文件，目录交换的改变也会先被缓存，必须对目录也强制刷新
                
                if fsync:
                    with suppress(PermissionError):
                        # 注意目录是.parent
                        fd = os.open(str(file_path.parent),os.O_RDONLY)
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
            except BaseException as e:
                # 删除临时文件，missing_ok表示允许文件不存在
                tmp_file_path.unlink(missing_ok=True)
                # 不用 logger.exception——全栈回溯刷到终端是噪音
                # （会话保存在此失败是预期可恢复路径：返回 False，
                # 调用方知道没落盘）。异常对象本身已带足够诊断信息。
                logger.error("会话保存失败 [%s]: %s: %s",
                             session.key, type(e).__name__, str(e)[:200])
                return False
        else:
            # sessions 路径被文件占位——正常运行时不会发生（初始化即建目录），
            # 防御性返回 False，不崩（UnboundLocalError 事故）
            logger.error("sessions 目录不可用: %s", self.sessions_dir)
            return False

        # 刷新缓存的session，如果没缓存，这行会将其加入到缓存
        self._cached_session[key]=session

        # 检查并返回文件是否保存成功
        return True

    def get_session_token_cost(
            self,
            session_key:str,
    ):
        session=self.get_or_create(session_key=session_key)
        return session.token_cost

    def list_session_keys(self)->list[str]:
        """列出磁盘上所有 session key，按修改时间倒序。"""
        if not self.sessions_dir.is_dir():
            return []
        files=sorted(
            (p for p in self.sessions_dir.iterdir() if p.suffix==".jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [p.stem for p in files]


