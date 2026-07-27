# 会员体系上线与回滚手册

## 发布顺序

1. 部署兼容后端，保持 `HQ_MEMBERSHIP_ENFORCEMENT_ENABLED=0`。
2. 验证 `/api/auth/me` 返回会员状态和服务端点数购买折扣。
3. 上传并审核包含会员展示、会员拦截和邀请注册的小程序版本。
4. 只读导出存量会员候选名单，人工确认后记录 CSV 的 SHA256。
5. 备份生产数据库，再使用显式名单执行存量迁移。
6. 对数据库快照运行上线检查，所有 blocker 清零后才能开总闸。
7. 设置 `HQ_MEMBERSHIP_ENFORCEMENT_ENABLED=1`，按 ship 规范部署并重启认证服务。
8. 使用非会员、体验官、合伙人、发起人四个受控账号验收。

严禁先开总闸再发布小程序，也不得直接修改生产服务器代码。

## 存量名单

候选发现只读：

```bash
python scripts/backfill_launch_experience_members.py \
  --db /path/users.db \
  --discovery-out /secure/path/candidates.csv
```

人工确认 `approved=yes` 后记录 SHA256，再执行：

```bash
python scripts/backfill_launch_experience_members.py \
  --db /path/users.db \
  --manifest /secure/path/approved.csv \
  --manifest-sha256 <sha256> \
  --apply \
  --confirm UPGRADE-EXPERIENCE-MEMBERS
```

正式执行必须使用显式名单，并由脚本自动生成迁移前备份。生产名单、数据库和凭证不得提交到 Git。

## 上线检查

对生产数据库的只读副本运行：

```bash
python scripts/check_membership_launch_readiness.py \
  --db /path/users.db \
  --json
```

退出码 `0` 且 `ready=true` 才能进入开总闸步骤。以下情况会阻断：

- 缺少会员、邀请或奖励台账表。
- `users` 缺少会员字段。
- 存在未知会员等级。
- 有效会员缺少免费音色槽位权益。
- 同一被邀请人存在重复邀请关系。

## 必测业务

- `/api/auth/me` 的三级会员、过期会员和非会员状态。
- 1000/2000/5000 点套餐的原价、7.5 折和 5.5 折报价。
- 客户端传入伪造金额时，服务端订单金额不变。
- 支付回调金额不匹配不得到账，重复回调不得重复到账。
- 非会员生成和点数购买返回 `403 membership_required`。
- 任务失败全额退点，退款重试不重复加点。
- 邀请关系只能绑定一次，会员等级不得超过邀请人允许范围。
- 邀请奖励进入独立奖励点数台账，不改变可消费点数。
- 公网访问内部点数及音色权益接口仍返回 404。

## 回滚

发现误拦截、会员等级错误、支付金额异常或小程序不可用时：

1. 将 `HQ_MEMBERSHIP_ENFORCEMENT_ENABLED` 恢复为 `0`。
2. 仅重启认证服务并验证非会员恢复使用。
3. 保留会员、邀请和审计数据，停止继续迁移。
4. 代码问题通过 revert PR 回滚并按 ship 规范重新部署。
5. 只有确认迁移名单错误时，才在停写窗口使用迁移前备份恢复数据库。
