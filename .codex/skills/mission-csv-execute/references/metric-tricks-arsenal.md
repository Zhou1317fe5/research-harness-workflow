# 科研 Trick 攻坚参考手册 (Metric Tricks Arsenal)

本手册专为视觉表征学习、小样本及跨域密集预测、测试时自适应（TTA）等科研实验设计。当探索阶段初测指标微亏（距离目标差 0.5~1.5 点）时，本手册提供即插即用的工程与算法补丁，用于就地攻坚拉升指标，保持大故事定力，严禁轻易推翻已确立的核心学术叙事。

---

## 阶段一：输入与数据流预处理级 Trick

### Trick 1: 掩码形态学腐蚀与边缘距离加权
- **作用**：消除标注边界的混合像素污染，提取纯净前景表征。
- **场景**：模糊边界、高噪点背景、细粒度病灶。
- **代码实现**：
```python
import cv2
import torch

def get_soft_foreground_mask(mask: torch.Tensor, kernel_size: int = 3) -> torch.Tensor:
    """mask: [B, 1, H, W] float or bool"""
    device = mask.device
    mask_np = mask.squeeze(1).cpu().numpy().astype("uint8")
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    soft_masks = []
    for m in mask_np:
        eroded = cv2.erode(m, kernel, iterations=1)
        dist = cv2.distanceTransform(eroded, cv2.DIST_L2, 5)
        if dist.max() > 0:
            dist = dist / dist.max()
        else:
            dist = eroded.astype("float32")
        soft_masks.append(torch.from_numpy(dist))
    return torch.stack(soft_masks, dim=0).unsqueeze(1).to(device)
```

---

### Trick 2: 保持长宽比的反射/零填充 (Aspect-Ratio Preserving Pad)
- **作用**：避免强制统一尺度导致的几何拉伸变形，保护特征局部拓扑。
- **代码实现**：
```python
import torch
import torch.nn.functional as F

def letterbox_pad(img: torch.Tensor, target_size: int = 448):
    """img: [C, H, W]"""
    _, h, w = img.shape
    scale = target_size / max(h, w)
    new_h, new_w = int(h * scale), int(w * scale)
    resized = F.interpolate(img.unsqueeze(0), size=(new_h, new_w), mode='bilinear', align_corners=False).squeeze(0)
    pad_h = target_size - new_h
    pad_w = target_size - new_w
    top, bottom = pad_h // 2, pad_h - (pad_h // 2)
    left, right = pad_w // 2, pad_w - (pad_w // 2)
    padded = F.pad(resized, (left, right, top, bottom), mode='reflect')
    return padded, (scale, top, left)
```

---

## 阶段二：骨干网络与特征提取级 Trick

### Trick 3: 深度多层特征残差跳连融合 (Multi-Layer Skip Fusion)
- **作用**：融合深层语义特征与浅层细粒度空间特征，平衡全局定位与边界精度。
- **代码实现**：
```python
def extract_multi_layer_features(backbone, x, layers=[-1, -3, -5], weights=[0.5, 0.3, 0.2]):
    features = backbone.get_intermediate_layers(x, n=max(abs(l) for l in layers))
    selected = [features[l] for l in layers]
    fused = sum(w * f for w, f in zip(weights, selected))
    return F.normalize(fused, p=2, dim=-1)
```

---

### Trick 4: 通道级球形 L2 重归一化 (Channel-wise Spherical Renormalization)
- **作用**：抑制预训练模型中个别高方差主导通道，防止背景相似度淹没微弱前景。
- **代码实现**：
```python
def spherical_norm(feat: torch.Tensor, eps: float = 1e-6) -> torch.Tensor:
    """feat: [B, C, H, W]"""
    mean = feat.mean(dim=1, keepdim=True)
    centered = feat - mean
    return F.normalize(centered, p=2, dim=1, eps=eps)
```

---

### Trick 5: 浅层边缘自相似性引导超分辨率 (Guided Upsampling)
- **作用**：替代粗暴双线性插值，利用低层特征自相似图消除低分辨率特征恢复时的块状马赛克。

---

## 阶段三：表征与原型校准级 Trick

### Trick 6: 自支持双阶段原型校准 (Self-Support Prototype Rectification, SSP)
- **作用**：缓解跨域时参考样本与测试样本之间的分布偏移。
- **代码实现**：
```python
def rectify_prototype_ssp(query_feat, support_proto, alpha=0.5, conf_thresh=0.75):
    """
    query_feat: [B, C, H, W]
    support_proto: [B, C, 1, 1]
    """
    coarse_sim = (F.normalize(query_feat, dim=1) * F.normalize(support_proto, dim=1)).sum(dim=1, keepdim=True)
    prob = torch.sigmoid(coarse_sim * 10.0)
    mask = (prob > conf_thresh).float()
    if mask.sum() > 10:
        query_proto = (query_feat * mask).sum(dim=[-2, -1], keepdim=True) / (mask.sum(dim=[-2, -1], keepdim=True) + 1e-6)
        rectified_proto = alpha * support_proto + (1.0 - alpha) * query_proto
    else:
        rectified_proto = support_proto
    return rectified_proto
```

---

### Trick 7: 多元硬背景负原型挖掘 (Hard Background Mining)
- **作用**：聚类提取 2~3 个局部背景硬负原型，与正原型竞争，降低复杂背景假阳性。

---

### Trick 8: 贝叶斯不确定性加权原型 (Bayesian Variance-Aware Prototype)
- **作用**：采用特征通道对角方差做马氏距离逆加权，降低不稳定特征维度的负面干扰。

---

## 阶段四：相似度度量与匹配层 Trick

### Trick 9: 动态特征方差温度缩放 (Dynamic Variance Temperature Scaling)
- **代码实现**：
```python
def adaptive_cosine_similarity(query_feat, proto, tau_0=0.1):
    q_norm = F.normalize(query_feat, p=2, dim=1)
    p_norm = F.normalize(proto, p=2, dim=1)
    raw_sim = (q_norm * p_norm).sum(dim=1, keepdim=True)
    variance = query_feat.std(dim=1, keepdim=True)
    tau = tau_0 * (1.0 + torch.tanh(variance))
    return raw_sim / tau
```

---

### Trick 10: 离散最优传输全局几何对齐 (Sinkhorn Optimal Transport)
- **作用**：引入全局质量守恒约束，避免多对一局部贪婪错配。

---

### Trick 11: 互惠近邻过滤 (Mutual Nearest Neighbor Filter)
- **作用**：双向 Top-K 互惠约束，自动修剪单向孤立错误关联。

---

## 阶段五：测试时自适应 (TTA) 级 Trick

### Trick 12: 零参数输入嵌入流微调 (Parameter-Free Input Embedding Adaptation)
- **作用**：冻结全部网络权重，仅反向更新输入前置 Token 1~3 步，彻底杜绝权重崩溃。
- **代码实现**：
```python
def test_time_adapt_embedding(model, query_img, steps=2, lr=1e-3):
    model.eval()
    for p in model.parameters():
        p.requires_grad = False
    embed = model.get_input_embedding(query_img).detach()
    embed.requires_grad = True
    optimizer = torch.optim.SGD([embed], lr=lr, momentum=0.9)
    for _ in range(steps):
        optimizer.zero_grad()
        logits = model.forward_with_embedding(embed)
        prob = torch.sigmoid(logits)
        entropy = -(prob * torch.log(prob + 1e-7) + (1 - prob) * torch.log(1 - prob + 1e-7)).mean()
        entropy.backward()
        optimizer.step()
    with torch.no_grad():
        final_logits = model.forward_with_embedding(embed)
    return torch.sigmoid(final_logits)
```

---

### Trick 13: 目标面积先验反塌陷正则 (Area Prior Regularization)
- **作用**：在 TTA 损失中加入面积先验惩罚，杜绝全 0 或全 1 模式塌陷。
- **代码实现**：
```python
def area_prior_loss(pred_prob, reference_mask, weight=2.0):
    ref_ratio = reference_mask.float().mean()
    pred_ratio = pred_prob.mean()
    return weight * torch.abs(pred_ratio - ref_ratio)
```

---

### Trick 14: 冷启动自蒸馏一致性 (Cold-Start Distillation)
- **作用**：TTA 优化过程中与初始冷启动预测保持 KL 散度一致性，防止过拟合扰动。

---

## 阶段六：输出后处理与判决层 Trick (终端保分)

### Trick 15: 预测概率直方图动态 Otsu 双峰阈值 (Dynamic Otsu Thresholding)
- **作用**：解决固定阈值 `prob > 0.5` 在微弱信号样本上导致的全部漏检。
- **代码实现**：
```python
import cv2
import numpy as np

def dynamic_threshold_otsu(prob_tensor: torch.Tensor, fallback_thresh: float = 0.5) -> torch.Tensor:
    """prob_tensor: [B, 1, H, W] in [0, 1]"""
    device = prob_tensor.device
    probs_np = prob_tensor.squeeze(1).cpu().numpy()
    binary_masks = []
    for p in probs_np:
        max_v = p.max()
        if max_v < 0.2:
            binary_masks.append(p > fallback_thresh)
            continue
        p_uint8 = (p * 255).astype('uint8')
        otsu_val, _ = cv2.threshold(p_uint8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        dynamic_t = max(min(otsu_val / 255.0, 0.65), 0.25)
        binary_masks.append(p > dynamic_t)
    return torch.from_numpy(np.stack(binary_masks)).unsqueeze(1).to(device)
```

---

### Trick 16: 自注意力亲和力自平滑 (Affinity Smoothing)
- **作用**：利用自注意力矩阵做一次轻量矩阵乘法，消除目标内部空洞与孤立飞点。

---

### Trick 17: 连通域微小孤立噪点过滤与孔洞填平 (Morphological Area Filtering)
- **作用**：消除面积小于总面积 0.5% 的孤立误报，提升边界紧致度。

---

## 快速自救配方表

| 现象 | 推荐组合 | 预期挽回幅度 |
| :--- | :--- | :--- |
| **指标差 0.5~1.2 点达标** | **Trick 15（动态 Otsu 阈值）+ Trick 1（掩码腐蚀）+ Trick 4（L2 归一）** | +1.2 ~ +2.0 点 |
| **跨域泛化出现较大回撤** | **Trick 6（自支持校准 SSP）+ Trick 7（多元负原型）** | +2.0 ~ +4.0 点 |
| **测试自适应（TTA）越调越差** | **Trick 13（面积先验正则）+ Trick 14（冷启动蒸馏）** | 消除崩塌并稳步回升 |
| **小目标大面积漏检** | **Trick 3（浅层残差融合）+ Trick 9（动态方差温度）** | +1.5 ~ +2.5 点 |
| **预测掩码边缘毛糙、空洞多** | **Trick 16（亲和力平滑）+ Trick 17（连通域孔洞填平）** | +0.8 ~ +1.6 点 |
