"""test_p18_llm_provider.py —— 模型供应商切换的验收（纯配置逻辑，不联网）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p18_llm_provider.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p18_llm_provider

在测什么：
    build_llm_cfg() 把"哪个供应商 + 哪个模型"拼成 qwen-agent 认的 cfg。

    ★ 为什么要测这个？
      换供应商的坑全在配置形状上（要不要 model_type、key 从哪读、默认模型是什么），
      而且错了只有等真调用才报。把它做成纯函数就能在这里一秒测完。

哪些东西【不】在这里测（因为要真 key）：
    · 真的能连通 DeepSeek —— 那是 --provider deepseek 手动跑一次的事
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.llm import PROVIDERS, build_llm_cfg  # noqa: E402


def _raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:                                  # noqa: BLE001
        raise AssertionError(f'期望 {exc_type.__name__}，实际 {type(exc).__name__}: {exc}') from exc
    raise AssertionError(f'期望抛 {exc_type.__name__}，但什么都没抛')


# ============================================================================
#  默认路径：dashscope（不能把原来的用法搞坏）
# ============================================================================

def test_dashscope_default_unchanged():
    """不传任何参数时，行为要和以前一模一样：qwen-max + dashscope。"""
    cfg = build_llm_cfg(api_key='sk-test')
    assert cfg['model'] == 'qwen-max'
    assert cfg['model_server'] == 'dashscope'
    assert cfg['api_key'] == 'sk-test'
    assert 'model_type' not in cfg, 'dashscope 不需要 model_type，加了反而绕路'


# ============================================================================
#  DeepSeek
# ============================================================================

def test_deepseek_cfg_shape():
    """DeepSeek 是 OpenAI 兼容接口：必须带 model_type='oai' + model_server。"""
    cfg = build_llm_cfg(provider='deepseek', api_key='sk-deepseek')
    assert cfg == {
        'model': 'deepseek-chat',
        'model_server': 'https://api.deepseek.com/v1',
        'model_type': 'oai',
        'api_key': 'sk-deepseek',
    }


def test_deepseek_default_model_is_chat_not_reasoner():
    """★ deepseek-reasoner(R1) 不支持 function calling —— 默认值必须选 chat。"""
    assert PROVIDERS['deepseek']['default_model'] == 'deepseek-chat'


def test_model_can_be_overridden():
    cfg = build_llm_cfg('deepseek-chat', 'deepseek', api_key='k')
    assert cfg['model'] == 'deepseek-chat'


def test_base_url_override():
    """自建 OpenAI 兼容服务：地址可覆盖，但 model_type 仍然要对。"""
    cfg = build_llm_cfg(provider='deepseek', api_key='k',
                        base_url='http://127.0.0.1:7905/v1')
    assert cfg['model_server'] == 'http://127.0.0.1:7905/v1'
    assert cfg['model_type'] == 'oai'


# ============================================================================
#  key 的来源
# ============================================================================

def test_key_read_from_env(monkeypatch=None):
    """key 默认从该 provider 对应的环境变量读。"""
    old = os.environ.get('DEEPSEEK_API_KEY')
    os.environ['DEEPSEEK_API_KEY'] = 'sk-from-env'
    try:
        cfg = build_llm_cfg(provider='deepseek')
        assert cfg['api_key'] == 'sk-from-env'
    finally:
        if old is None:
            os.environ.pop('DEEPSEEK_API_KEY', None)
        else:
            os.environ['DEEPSEEK_API_KEY'] = old


def test_api_key_env_can_be_renamed():
    """可以用 --api-key-env 指定别的变量名（比如公司统一的 LLM_API_KEY）。"""
    old = os.environ.get('MY_LLM_KEY')
    os.environ['MY_LLM_KEY'] = 'sk-custom'
    try:
        cfg = build_llm_cfg(provider='deepseek', api_key_env='MY_LLM_KEY')
        assert cfg['api_key'] == 'sk-custom'
    finally:
        if old is None:
            os.environ.pop('MY_LLM_KEY', None)
        else:
            os.environ['MY_LLM_KEY'] = old


def test_missing_key_raises_with_actionable_message():
    """没 key 时要在【装配阶段】就报错，而不是等真调用报 401。"""
    old = os.environ.pop('DEEPSEEK_API_KEY', None)
    try:
        try:
            build_llm_cfg(provider='deepseek')
        except ValueError as exc:
            assert 'DEEPSEEK_API_KEY' in str(exc), f'报错里要写清是哪个变量：{exc}'
        else:
            raise AssertionError('没 key 居然没报错')
    finally:
        if old is not None:
            os.environ['DEEPSEEK_API_KEY'] = old


def test_require_key_false_allows_offline_construction():
    """单元测试/离线场景可以不要 key。"""
    cfg = build_llm_cfg(provider='deepseek', require_key=False)
    assert 'api_key' not in cfg


# ============================================================================
#  错误
# ============================================================================

def test_unknown_provider_raises():
    _raises(ValueError, lambda: build_llm_cfg(provider='openai-not-supported',
                                              require_key=False))


def test_provider_table_has_expected_entries():
    """表里有哪些供应商是公开契约，别被误删。"""
    assert set(PROVIDERS) == {'dashscope', 'deepseek'}
    for name, spec in PROVIDERS.items():
        assert spec['default_model'], f'{name} 缺 default_model'
        assert spec['model_server'], f'{name} 缺 model_server'
        assert spec['api_key_env'], f'{name} 缺 api_key_env'


# ============================================================================
#  脚本模式入口
# ============================================================================

def main() -> None:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'  [ok]   {name}')
            passed += 1
        except Exception as exc:                              # noqa: BLE001
            print(f'  [FAIL] {name}  ->  {type(exc).__name__}: {exc}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
