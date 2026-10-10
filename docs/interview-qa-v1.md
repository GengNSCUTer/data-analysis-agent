# 面试问答

回答只使用已经冻结的实验和当前产品路径。细节见 [模型实验卡](post-training/experiments/project-v1-model-card.md) 和 [产品请求架构](architecture/product-request-architecture-v1.md)。

**SFT 在学什么？** 给定中文问题、Catalog、QueryPlan 和 ResultContract，续写一条 PostgreSQL SQL。Prompt 的 token 可见，但 label 是 -100，不参与 loss。

**为什么用 LoRA 而不是全参？** Base 冻结，只训练约 1.182% 的参数。这是 1.5B 模型在现有 GPU 上的微调方式，不是说全参一定更差。

**AdamW 做了什么？** 它是这次 SFT 的优化器，学习率 `1e-4`，weight decay `0.01`。loss 下降只用于训练期选 checkpoint，不等于业务答案正确。

**为什么没做 DPO？** DPO 需要同一 Prompt 上的 Gold SQL 和模型真实生成、并且被治理或 Gold 判错的 SQL。两批各 48 次采样没有这种负例。没有负例就不训练。

**怎样防止数据泄漏？** protected final test 不进入训练、验证、few-shot 或合成种子。checkpoint 在训练期按 validation 选定，看过 final test 之后不改选。

**Gold denotation 是什么？** 候选先过策略、只读执行和结果合同。合同通过之后，再比结果的列顺序、行和数值是否与 Gold 一致。合同通过本身不是 Gold 一致。

**模型和执行治理怎么分工？** 模型只提出候选 SQL。能不能执行、能不能把数字给用户，由 SqlPolicy、只读账号和 ResultValidator 决定。网页里的默认模型仍是在线模型，本地 Adapter 还没接进去。
