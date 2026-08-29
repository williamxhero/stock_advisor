# 由版本化认知策略拥有 Effort

Provider 配置、Broker 路由和应用内认知策略曾都可能提供 effort，容易产生多个相互覆盖的默认值。现在由本地版本化 `CognitiveEffortPolicy` 在每次调用前形成不可变 EffortDecision；Provider Broker 只执行逐次请求并选择模型与 Provider。这样 effort 可以按任务场景经过同包 shadow、评测、治理和回执持续进化，同时不会让远端执行配置取得生产策略权威。
