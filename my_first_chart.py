import matplotlib.pyplot as plt

plt.rcParams["font.family"] = "Malgun Gothic" 

study_time = [3, 2, 0.5, 3, 2]

fig, ax = plt.subplots(figsize=(7, 4))
ax.plot(study_time)

for i, p in enumerate(study_time):
    ax.annotate(f"{p:,}", (i, p), textcoords="offset points", xytext=(0, 8), ha="center")
    
ax.set_title("순공시간 기록")
fig.savefig("my_first_chart.png")