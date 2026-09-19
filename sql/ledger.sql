-- 运行账本：技术设计文档 §7.3 规定的两张自建表。
--
-- 本文件不由应用自动执行——`OpenGaussAsyncSaver.setup()` 只建 LangGraph 自己的
-- checkpoint 相关表，业务自建表不走应用 DDL，需要 DBA 或部署流程手工执行本文件。
--
-- 隐私口径：这两张表只存 hash 和计数，不存用户提问原文、不存工具入参原文、
-- 不存条款正文（见 CLAUDE.md 日志红线，同一条口径也适用于落库）。

CREATE TABLE IF NOT EXISTS t_agent_run (
    run_id          VARCHAR(64)   PRIMARY KEY,        -- 本模块生成的 uuid4 hex
    thread_id       VARCHAR(128)  NOT NULL,            -- 会话 ID，对应 LangGraph 的 thread_id
    tenant_id       VARCHAR(64),                       -- 当前无租户概念，永远 NULL，不得编造
    actor_id        VARCHAR(128)  NOT NULL,            -- 发起者标识，取 Identity.isolate_key
    run_type        VARCHAR(32)   NOT NULL,            -- 目前固定 'CHAT'
    status          VARCHAR(32)   NOT NULL,            -- RUNNING / SUCCEEDED / FAILED / ABORTED（客户端中途断开）
    model_name      VARCHAR(128),                      -- TAX_AGENT_MODEL
    prompt_version  VARCHAR(64),                        -- SYSTEM_PROMPT 的内容 hash，不是手工版本号
    input_hash      VARCHAR(64),                        -- 用户提问的 hash，只存摘要，原文不落库
    usage_json       TEXT,                              -- token 用量，JSON 字符串；拿不到就 NULL，不编
    quality_json     TEXT,                              -- 质量信号（如引用校验结果），JSON 字符串
    trace_id        VARCHAR(64),                        -- 链路 ID，跨表联查键
    started_at      TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at     TIMESTAMP WITH TIME ZONE,
    latency_ms      INTEGER,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_t_agent_run_thread_started ON t_agent_run (thread_id, started_at);
CREATE INDEX IF NOT EXISTS idx_t_agent_run_trace_id ON t_agent_run (trace_id);

-- 主键选 (run_id, seq) 而不是 tool_call_id：tool_call_id 由模型侧生成，跨 run
-- 不保证唯一，而且工具在请求之外被调用（模块自检、evals）时它是空串，没资格当主键。
-- seq 从 0 递增，顺带把调用顺序变成可查的一列。
CREATE TABLE IF NOT EXISTS t_agent_tool_call (
    run_id          VARCHAR(64)   NOT NULL,            -- 关联 t_agent_run.run_id
    seq             INTEGER       NOT NULL,            -- 本次 run 内的调用顺序，从 0 开始
    tool_call_id    VARCHAR(128),                       -- 模型侧生成的 ID，可能为空（自检/evals 场景）
    tool_name       VARCHAR(64)   NOT NULL,            -- search_regulation / fetch_clause
    args_hash       VARCHAR(64),                        -- 入参的 hash，只存摘要，原文（含检索词）不落库
    status          VARCHAR(32)   NOT NULL,            -- OK / NO_EVIDENCE / NOT_FOUND / INVALID_AS_OF / UNSUPPORTED_AS_OF / ERROR
    latency_ms      INTEGER,
    evidence_count  INTEGER,
    error_code      VARCHAR(64),                        -- 仅 status=ERROR 时有值，异常类名
    trace_id        VARCHAR(64),
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- §7.4 要求自建表统一带这一列；本表实际只 INSERT 不 UPDATE
    PRIMARY KEY (run_id, seq),
    -- §7.4 的 ER 图画的就是 run ||--o{ tool : run_id。写库是同一个事务里先 run 后
    -- tool_call，约束一定满足；有了它，孤儿 tool_call 行在物理上就不可能出现。
    -- 部署环境若不允许外键，删掉这一行即可，应用侧不依赖它。
    CONSTRAINT fk_t_agent_tool_call_run FOREIGN KEY (run_id) REFERENCES t_agent_run (run_id)
);

CREATE INDEX IF NOT EXISTS idx_t_agent_tool_call_tool_call_id ON t_agent_tool_call (tool_call_id);
