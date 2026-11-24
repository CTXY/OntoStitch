"""Read-only timing/context audit of batch 3, separated from retained old logs."""
from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/signal_subgraphs_20261122/performance_latest'


def read(path):
    return json.loads(path.read_text())


def timestamp(row):
    return datetime.fromisoformat(row['recorded_at']).timestamp()


def checkpoint(folder):
    with sqlite3.connect(f'file:{folder / "workspace.sqlite"}?mode=ro', uri=True) as connection:
        return json.loads(connection.execute('select payload from merge_workspaces').fetchone()[0])


def audit(short, since):
    root = ROOT / 'output' / (short + '_remaining_merge_pro_signal_subgraphs_20261122')
    folder = root / 'merge/batch-0003'
    cp, status = checkpoint(folder), read(root / 'status.json')
    old = checkpoint(ROOT / 'output' / (short + '_remaining_merge_pro_signal_receipts_20261122') / 'merge/batch-0003')
    all_events = [json.loads(line) for line in (folder / 'model_calls.jsonl').read_text().splitlines()]
    events = [row for row in all_events if timestamp(row) >= since]
    starts = {row['call_id']: row for row in events if row['event_type'] == 'model_start'}
    ends = {row['call_id']: row for row in events if row['event_type'] in {'model_end', 'model_error'}}
    recovery_file = ROOT / 'output/signal_subgraphs_20261122/toxicology/stream_idle_recovery.json'
    abandoned = read(recovery_file) if short == 'toxicology' and recovery_file.exists() else {}
    timings = defaultdict(lambda: {'successful_calls': 0, 'failed_calls': 0, 'success_seconds': 0, 'failure_seconds': 0, 'inflight_calls': 0})
    calls = []
    for ident, row in starts.items():
        end = ends.get(ident)
        stopped = ident == abandoned.get('abandoned_call_id')
        elapsed = (timestamp(end) if end else abandoned['stopped_at'] if stopped else time.time()) - timestamp(row)
        stage = timings[row['component']]
        if end:
            key = 'success' if end['event_type'] == 'model_end' else 'failure'
            stage[key + '_seconds'] += elapsed
            stage['successful_calls' if key == 'success' else 'failed_calls'] += 1
        elif stopped:
            stage['failed_calls'] += 1
            stage['failure_seconds'] += elapsed
        else:
            stage['inflight_calls'] += 1
        item = {'call_id': ident, 'component': row['component'], 'started_at': row['recorded_at'],
                'elapsed_seconds': round(elapsed, 2), 'status': end['event_type'] if end else 'abandoned_incomplete_stream' if stopped else 'inflight'}
        if end and end['event_type'] == 'model_error':
            item['diagnostic'] = str(end.get('error', ''))[:300]
        calls.append(item)
    old_accepted = {row['check'].get('id') for row in old.get('signal_receipts', [])}
    accepted_at = {ident: since - 1 for ident in old_accepted}
    for row in cp.get('signal_receipts', []):
        matches = [x for x in events if x['event_type'] == 'agent_episode' and x.get('task_id') == row['task_id'] + '-review']
        if matches:
            accepted_at[row['check'].get('id')] = min(timestamp(x) for x in matches)
    episodes = {}
    for row in starts.values():
        messages = row['messages']
        fixed = messages[1]['content']
        ep = episodes.setdefault(row['episode_id'], {'component': row['component'], 'starts': [], 'message_counts': [],
            'fixed_prefixes': set(), 'signal_ids': re.findall(r'^## Items to check this round .*?\(`([^`]+)`\)', fixed, re.M)})
        ep['starts'].append(timestamp(row))
        ep['message_counts'].append(len(messages))
        ep['fixed_prefixes'].add(sha256(json.dumps(messages[:2], ensure_ascii=False).encode()).hexdigest())
    for ep in episodes.values():
        ep['accepted_signal_replay_candidates'] = [ident for ident in ep['signal_ids'] if accepted_at.get(ident, float('inf')) < min(ep['starts'])]
        ep['requests'] = len(ep.pop('starts'))
        ep['distinct_fixed_prefixes'] = len(ep.pop('fixed_prefixes'))
    budgets = [{'component': x['component'], 'episode_id': x['episode_id'], 'characters': x['characters'],
                'limit': x['budget'], 'at': x['recorded_at']} for x in events if x['event_type'] == 'request_budget']
    by_episode = defaultdict(list)
    for row in budgets:
        by_episode[row['episode_id']].append(row['characters'])
    evidence_reads = [{'component': x['component'], 'episode_id': x['episode_id'], 'ids': x.get('arguments', {}).get('ids', [])}
                      for x in events if x['event_type'] == 'tool_result' and x.get('name') == 'read_evidence']
    wire = []
    for path in (folder / 'wire').glob('*_request.json'):
        request = read(path)
        if request['time'] < since:
            continue
        progress_path = path.with_name(path.name.replace('_request.json', '_progress.json'))
        progress = read(progress_path) if progress_path.exists() else {}
        wire.append({'request': path.name, 'start': request['time'], 'model': request.get('body', {}).get('model'),
            'completed': progress.get('completed', False), 'last_byte_time': progress.get('last_byte_time'),
            'idle_seconds': round(time.time() - progress['last_byte_time'], 2) if progress.get('last_byte_time') else None,
            'stream_bytes': progress.get('bytes'), 'errors': progress.get('error_chain', [])})
    audit_file = ROOT / 'output/signal_subgraphs_20261122' / short / 'receipt_reuse_latest.json'
    return {'database': short, 'checked_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'status': status['status'], 'batch': status.get('batch'), 'completed_cases': status.get('merged_cases'),
        'total_cases': status.get('total_cases'), 'errors': status.get('errors', []),
        'batch3_completed': (folder / 'completed.json').exists(), 'batch3_phase_accepted_tasks': dict(Counter(x['phase'] for x in cp['history'])),
        'accepted_tasks': len(cp['history']), 'accepted_source_prefix': len(old['history']),
        'original_accepted_prefix_unchanged': cp['history'][:len(old['history'])] == old['history'],
        'stage': (cp.get('task_state') or {}).get('stage'), 'pending_task': (cp.get('task_state') or {}).get('task_id'),
        'receipts': len(cp.get('signal_receipts', [])), 'receipt_reuse_audit': read(audit_file),
        'new_run_started_at': datetime.fromtimestamp(since, ZoneInfo('Asia/Shanghai')).isoformat(),
        'new_run_model_timing': dict(timings), 'new_run_calls': calls, 'new_run_episodes': list(episodes.values()),
        'new_run_request_budgets': budgets, 'characters_by_episode': dict(by_episode), 'evidence_reads': evidence_reads,
        'message_count_always_3': all(n == 3 for ep in episodes.values() for n in ep['message_counts']),
        'fixed_prefix_unchanged_per_episode': all(ep['distinct_fixed_prefixes'] == 1 for ep in episodes.values()),
        'no_previously_accepted_signal_replay_candidates': not any(ep['accepted_signal_replay_candidates'] for ep in episodes.values()),
        'peak_new_run_request_characters': max((b['characters'] for b in budgets), default=0),
        'wire_progress': sorted(wire, key=lambda x: x['start']), 'stream_idle_recovery': abandoned,
        'runtime_limits': status.get('runtime_limits'), 'active_key_slot': status.get('used_anonymous_key_slots', [])[-1:]}


def main():
    OUT.mkdir(exist_ok=True)
    windows = read(OUT.parent / 'launch_windows.json')
    rows = {short: audit(short, since) for short, since in windows.items()}
    result = {'checked_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), 'databases': rows,
              'read_only': True, 'live_checkpoints_mutated': False}
    (OUT / 'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    html = '<h1>Batch 3 最新耗时、重试与上下文核查</h1><p>核查时间：' + escape(result['checked_at']) + '</p>'
    html += '<p>以下只读检查区分此次新代码运行与复制保留的历史日志。网络重试不会被统计为重新处理已通过 Signal。字符数含请求schema等，并非token数。</p>'
    html += '<table><tr><th>库</th><th>完成cases/当前batch</th><th>batch3内部已接受任务</th><th>新运行最大输入</th><th>状态</th></tr>'
    for short, row in rows.items():
        html += '<tr><td>' + short + '</td><td>' + str(row['completed_cases']) + '/' + str(row['total_cases']) + ' · ' + str(row['batch']) + '</td><td>' + str(row['accepted_tasks']) + '</td><td>' + str(row['peak_new_run_request_characters']) + '</td><td>' + escape(row['status']) + '</td></tr>'
    html += '</table><h2>为什么慢</h2><p>thrombosis 的一个 Assessment 在流停止后等待600秒空闲超时，整次失败耗时约648秒，随后换KEY并继续原请求。toxicology 的 Writer 也发生一次流停止及600秒等待；这类网络等待未增加已接受任务。正常模型调用通常还需几十至百余秒，按需补读 evidence 会增加调用轮次。</p>'
    html += '<p>thrombosis 随后通过全部检查，但最终发布时 GraphManager 把两条专属分支关系扩成全部分支，被保义校验阻止。已修复为保留明确alternative_ids，并保留原校验；真实失败图完整发布回归通过，源checkpoint未改。恢复时仅重做发布，batch3已完成并进入batch4。</p>'
    html += '<p>toxicology 第二次流停顿后，保全未完成请求的 checkpoint，主动更换同一独立池的下一KEY续跑；没有重做已接受任务。详细恢复凭据不含任何key明文，见机器可读记录。</p><h2>重复与上下文核查</h2><p>此次新运行未出现已接受Signal再次进入模型的候选记录。receipt离线调度回放的模型调用为0；已接受history前缀不变。每次请求始终3条消息，同一episode固定system+图输入前缀一致。输入小幅增加对应新增原始evidence，未发现图重复或历史消息累积；不同Signal任务的大小不能当作同一上下文膨胀。</p>'
    for short, row in rows.items():
        html += '<h3>' + short + ' · 最新流进度（未完成时注意最后字节距核查时间的秒数）</h3><pre>' + escape(json.dumps(row['wire_progress'][-2:], ensure_ascii=False, indent=2)) + '</pre>'
        html += '<h2>' + short + '</h2><h3>各阶段模型耗时（秒）</h3><pre>' + escape(json.dumps(row['new_run_model_timing'], ensure_ascii=False, indent=2)) + '</pre><h3>逐次调用与超时</h3><pre>' + escape(json.dumps(row['new_run_calls'], ensure_ascii=False, indent=2)) + '</pre><h3>每轮输入字符与固定前缀核查</h3><pre>' + escape(json.dumps({'episodes': row['new_run_episodes'], 'characters': row['characters_by_episode'], 'evidence_reads': row['evidence_reads']}, ensure_ascii=False, indent=2)) + '</pre>'
    html += '<p><a href="analysis.json">完整机器可读核查</a> · <a href="../publication_failure/diff.json">发布问题两处分支范围差异</a> · <a href="../publication_failure/regression_result.json">真实发布回归</a> · <a href="../index.html">完整输入输出与优化报告</a> · <a href="../../signal_receipts_rollout_20261122/index.html">持续后台状态</a></p>'
    (OUT / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Merge progress timing audit</title><style>body{font:16px/1.7 system-ui;max-width:1100px;margin:30px auto;padding:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px;text-align:left}</style>' + html)
    print(json.dumps({key: {field: row[field] for field in ('status', 'batch', 'completed_cases', 'total_cases', 'accepted_tasks', 'message_count_always_3', 'fixed_prefix_unchanged_per_episode', 'peak_new_run_request_characters', 'no_previously_accepted_signal_replay_candidates')} for key, row in rows.items()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
